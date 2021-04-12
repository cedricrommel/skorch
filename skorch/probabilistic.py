"""Integrate GPyTorch for Gaussian Processes

TODO: verify the assumptions being made and remove from here:

- The criterion always takes likelihood and module as input arguments
- Always optimize the negative objective function
- Need elaboration on how batching works - are distributions disjoint?

"""

import pickle
import re
import warnings

import gpytorch
import numpy as np
import torch

from skorch.net import NeuralNet
from skorch.dataset import CVSplit
from skorch.dataset import unpack_data
from skorch.callbacks import EpochScoring
from skorch.callbacks import EpochTimer
from skorch.callbacks import PassthroughScoring
from skorch.callbacks import PrintLog
from skorch.exceptions import SkorchWarning
from skorch.utils import check_is_fitted
from skorch.utils import get_dim
from skorch.utils import is_dataset
from skorch.utils import to_numpy


warnings.warn("The API of the Gaussian Process estimators is experimental and may "
              "change in the future", SkorchWarning)

__all__ = ['ExactGPRegressor', 'GPRegressor', 'GPBinaryClassifier']


class GPBase(NeuralNet):
    """Base class for all Gaussian Process estimators.

    Most notably, a GPyTorch compatible criterion and likelihood should be
    provided.

    """
    def __init__(
            self,
            module,
            *args,
            likelihood,
            criterion,
            train_split=None,
            **kwargs
    ):
        super().__init__(
            module,
            *args,
            criterion=criterion,
            train_split=train_split,
            **kwargs
        )
        self.likelihood = likelihood

    def initialize_module(self):
        """Initializes likelihood and module."""
        # pylint: disable=attribute-defined-outside-init

        ll_kwargs = self.get_params_for('likelihood')
        likelihood = self.likelihood
        is_initialized = isinstance(likelihood, torch.nn.Module)

        if not is_initialized or ll_kwargs:
            # likelihood needs to be initialized because it's not yet or because
            # its arguments changed
            if is_initialized:
                likelihood = type(likelihood)
            self.likelihood_ = likelihood(**ll_kwargs)

        super().initialize_module()
        return self

    def initialize_criterion(self):
        """Initializes the criterion."""
        # pylint: disable=attribute-defined-outside-init

        criterion_params = self.get_params_for('criterion')
        # criterion takes likelihood as first argument
        self.criterion_ = self.criterion(
            likelihood=self.likelihood_,
            model=self.module_,
            **criterion_params
        )
        return self

    def train_step_single(self, batch, **fit_params):
        """Compute y_pred, loss value, and update net's gradients.

        The module is set to be in train mode (e.g. dropout is
        applied).

        Parameters
        ----------
        batch
          A single batch returned by the data loader.

        **fit_params : dict
          Additional parameters passed to the ``forward`` method of
          the module and to the ``self.train_split`` call.

        Returns
        -------
        step : dict
          A dictionary ``{'loss': loss, 'y_pred': y_pred}``, where the
          float ``loss`` is the result of the loss function and
          ``y_pred`` the prediction generated by the PyTorch module.

        """
        step = super().train_step_single(batch, **fit_params)
        # To obtain the posterior, the likelihood must be applied on the output
        # of the module. This cannot be performed inside the module, because the
        # GPyTorch criteria apply the likelihood on the module output
        # themselves.
        step['y_pred'] = self.likelihood_(step['y_pred'])
        return step

    # pylint: disable=unused-argument
    def get_loss(self, y_pred, y_true, X=None, training=False):
        """Return the loss for this batch.

        Parameters
        ----------
        y_pred : torch tensor
          Predicted target values

        y_true : torch tensor
          True target values.

        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * scipy sparse CSR matrices
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        training : bool (default=False)
          Whether train mode should be used or not.

        Returns
        -------
        loss : torch Tensor (scalar)
          The loss to be minimized.

        """
        loss = super().get_loss(y_pred, y_true, X=X, training=training)
        if loss.dim() != 0:
            loss = loss.mean()
        return -loss

    def evaluation_step(self, batch, training=False):
        """Perform a forward step to produce the output used for
        prediction and scoring.

        Therefore, the module is set to evaluation mode by default
        beforehand which can be overridden to re-enable features
        like dropout by setting ``training=True``.

        Parameters
        ----------
        batch
          A single batch returned by the data loader.

        training : bool (default=False)
          Whether to set the module to train mode or not.

        Returns
        -------
        y_infer
          The prediction generated by the module.

        """
        self.check_is_fitted()
        Xi, _ = unpack_data(batch)
        with torch.set_grad_enabled(training), gpytorch.settings.fast_pred_var():
            self.module_.train(training)
            y_infer = self.infer(Xi)
            if isinstance(y_infer, tuple):  # multiple outputs:
                return (self.likelihood_(y_infer[0]),) + y_infer[1:]
            return self.likelihood_(y_infer)

    def forward(self, X, training=False, device='cpu'):
        """Gather and concatenate the output from forward call with
        input data.

        The outputs from ``self.module_.forward`` are gathered on the
        compute device specified by ``device`` and then concatenated
        using PyTorch :func:`~torch.cat`. If multiple outputs are
        returned by ``self.module_.forward``, each one of them must be
        able to be concatenated this way.

        Notes
        -----
        For Gaussian Process modules, the return value of the module is a
        distribution. These distributions are collected in a list (which may
        only contain a single element if just one batch was used). Distributions
        *cannot* be concatenated. Therefore, this method will just return the
        list of distributions.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * scipy sparse CSR matrices
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        training : bool (default=False)
          Whether to set the module to train mode or not.

        device : string (default='cpu')
          The device to store each inference result on.
          This defaults to CPU memory since there is genereally
          more memory available there. For performance reasons
          this might be changed to a specific CUDA device,
          e.g. 'cuda:0'.

        Returns
        -------
        y_infer : list of gpytorch.distributions.Distribution
          A list of distributions as generated by the module. The number of
          elements in this list will depend on the sample size of X and the
          batch size of the estimator.

        """
        y_infer = list(self.forward_iter(X, training=training, device=device))
        return y_infer

    def predict_proba(self, X):
        raise AttributeError("'predict_proba' is not implemented for {}".format(
            self.__class__.__name__
        ))

    def sample(self, X, n_samples, axis=-1):
        """Return samples conditioned on input data.

        The GP doesn't need to be fitted but it must be initialized.

        X : input data
          The samples where the GP is evaluated.

        n_samples : int
          The number of samples to return

        axis : int (default=-1)
          The concatenation axis of the samples. Since samples can come in
          batches, they must be concatenated.

        Returns
        -------
        samples : torch.Tensor
          Samples from the posterior distribution.

        """
        self.check_is_fitted()
        samples = [p.sample(torch.Size([n_samples])) for p in self.forward_iter(X)]
        return torch.cat(samples, axis=axis)

    def confidence_region(self, X, sigmas=2):
        """Returns 2 standard deviations above and below the mean.

        X : input data
          The samples where the GP is evaluated.

        sigmas : int (default=2)
          The number of standard deviations of the region.

        Returns
        -------
        lower : torch.Tensor
          The lower end of the confidence region.

        upper : torch.Tensor
          The upper end of the confidence region.

        """
        nonlin = self._get_predict_nonlinearity()
        lower, upper = [], []
        for yi in self.forward_iter(X):
            posterior = yi[0] if isinstance(yi, tuple) else yi
            mean = posterior.mean
            std = posterior.stddev
            std = std.mul_(sigmas)
            lower.append(nonlin(mean.sub(std)))
            upper.append(nonlin(mean.add(std)))

        lower = torch.cat(lower)
        upper = torch.cat(upper)
        return lower, upper

    def __getstate__(self):
        try:
            return super().__getstate__()
        except pickle.PicklingError as exc:
            msg = ("This GPyTorch model cannot be pickled. The reason is probably this:"
                   " https://github.com/pytorch/pytorch/issues/38137. "
                   "Try using 'dill' instead of 'pickle'.")
            raise pickle.PicklingError(msg) from exc


class _GPRegressorPredictMixin:
    """Mixin class that provides a predict method for GP regressors."""
    def predict(self, X, return_std=False, return_cov=False):
        """Returns the predicted mean and optionally standard deviation.

        Parameters
        ----------
        X : input data
          Input data where the GP is evaluated.

        return_std : bool (default=False)
          If True, the standard-deviation of the predictive distribution at the
          query points is returned along with the mean.

        return_cov : bool (default=False)
          This exists solely for sklearn compatibility and is not supported by
          skorch.

        Returns
        -------
        y_pred : numpy ndarray
          Mean of predictive distribution at the query points.

        y_std : numpy ndarray
          Standard deviation of predictive distribution at query points. Only
          returned when ``return_std`` is True.

        """
        if return_cov:
            msg = ("The 'return_cov' argument is not supported. Please try: "
                   "'posterior = next(gpr.forward_iter(X)); "
                   "posterior.covariance_matrix'.")
            raise NotImplementedError(msg)

        nonlin = self._get_predict_nonlinearity()
        y_preds, y_stds = [], []
        for yi in self.forward_iter(X, training=False):
            posterior = yi[0] if isinstance(yi, tuple) else yi
            y_preds.append(to_numpy(nonlin(posterior.mean)))
            if not return_std:
                continue

            y_stds.append(to_numpy(nonlin(posterior.stddev)))

        y_pred = np.concatenate(y_preds, 0)
        if not return_std:
            return y_pred

        y_std = np.concatenate(y_stds, 0)
        return y_pred, y_std


exact_gp_regr_doc_start = """Exact Gaussian Process regressor

    Use this specifically if you want to perform an exact solution to the
    Gaussian Process. This implies that the module should by a
    :class:`~gpytorch.models.ExactGP` module and you cannot use batching (i.e.
    batch size should be -1).

"""

exact_gp_regr_module_text = """

    Module : gpytorch.models.ExactGP (class or instance)
      The module needs to return a
      :class:`~gpytorch.distributions.MultivariateNormal` distribution.

"""

exact_gp_regr_criterion_text = """

    likelihood : gpytorch.likelihoods.GaussianLikelihood (class or instance)
      The likelihood used for the exact GP regressor. Usually doesn't need to be
      changed.

    criterion : gpytorch.mlls.ExactMarginalLogLikelihood
      The objective function to learn the posterior of of the GP regressor.
      Usually doesn't need to be changed.

"""

exact_gp_regr_batch_size_text = """

    batch_size : int (default=-1)
      Mini-batch size. For exact GPs, it must be set to -1, since the exact
      solution cannot deal with batching. To make use of batching, use
      :class:`.GPRegressor` in conjunction with a variational strategy.

"""

# this is the same text for exact and approximate GP regression
gp_regr_train_split_text = """

    train_split : None or callable (default=None)
      If None, there is no train/validation split. Else, train_split should be a
      function or callable that is called with X and y data and should return
      the tuple ``dataset_train, dataset_valid``. The validation data may be
      None. There is no default train split for GP regressors because random
      splitting is typically not desired, e.g. because there is a temporal
      relationship between samples.

"""

# this is the same text for all GPs
gp_likelihood_attribute_text = """

    likelihood_: torch module (instance)
      The instantiated likelihood.

"""


def get_exact_gp_regr_doc(doc):
    """Customizes the net docs to avoid duplication."""
    params_start_idx = doc.find('    Parameters\n    ----------')
    doc = doc[params_start_idx:]
    doc = exact_gp_regr_doc_start + " " + doc

    pattern = re.compile(r'(\n\s+)(module .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + exact_gp_regr_module_text + doc[end:]

    pattern = re.compile(r'(\n\s+)(criterion .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + exact_gp_regr_criterion_text + doc[end:]

    pattern = re.compile(r'(\n\s+)(batch_size .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + exact_gp_regr_batch_size_text + doc[end:]

    pattern = re.compile(r'(\n\s+)(train_split .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + gp_regr_train_split_text + doc[end:]

    doc = doc + gp_likelihood_attribute_text

    return doc


class ExactGPRegressor(_GPRegressorPredictMixin, GPBase):
    # pylint: disable=missing-docstring
    __doc__ = get_exact_gp_regr_doc(NeuralNet.__doc__)

    def __init__(
            self,
            module,
            *args,
            likelihood=gpytorch.likelihoods.GaussianLikelihood,
            criterion=gpytorch.mlls.ExactMarginalLogLikelihood,
            batch_size=-1,
            **kwargs
    ):
        super().__init__(
            module,
            *args,
            criterion=criterion,
            likelihood=likelihood,
            batch_size=batch_size,
            **kwargs
        )

    def initialize_module(self):
        """Initializes likelihood and module."""
        # pylint: disable=attribute-defined-outside-init

        # We need a custom implementation here because the module is initialized
        # with likelihood as an argument, which would not be passed otherwise.
        likelihood = self.likelihood
        ll_kwargs = self.get_params_for('likelihood')

        module = self.module
        module_kwargs = self.get_params_for('module')

        initialized_ll = isinstance(likelihood, torch.nn.Module)
        initialized_both = initialized_ll and isinstance(module, torch.nn.Module)

        if not initialized_ll or ll_kwargs:
            # likelihood needs to be initialized because it's not yet or because
            # its arguments changed
            if initialized_ll:
                likelihood = type(likelihood)
            self.likelihood_ = likelihood(**ll_kwargs)

        if 'likelihood' not in module_kwargs:
            module_kwargs['likelihood'] = self.likelihood_

        if not initialized_both or module_kwargs:
            # module needs to be initialized because it's not yet or because
            # the likelihood and/or its arguments changed
            if initialized_both:
                module = type(module)
            self.module_ = module(**module_kwargs)

        if not isinstance(self.module_, gpytorch.models.ExactGP):
            raise TypeError("{} requires 'module' to be a gpytorch.models.ExactGP."
                            .format(self.__class__.__name__))
        return self


gp_regr_doc_start = """Gaussian Process regressor

    Use this for variational and approximate Gaussian process regression. This
    implies that the module should by a :class:`~gpytorch.models.ApproximateGP`
    module.

"""

gp_regr_module_text = """

    Module : gpytorch.models.ApproximateGP (class or instance)
      The GPyTorch module; in contrast to exact GP, the return distribution does
      not need to be Gaussian.

"""

gp_regr_criterion_text = """

    likelihood : gpytorch.likelihoods.GaussianLikelihood (class or instance)
      The likelihood used for the exact GP regressor. Usually doesn't need to be
      changed.

    criterion : gpytorch.mlls.VariationalELBO
      The objective function to learn the approximate posterior of of the GP
      regressor.

"""


def get_gp_regr_doc(doc):
    """Customizes the net docs to avoid duplication."""
    params_start_idx = doc.find('    Parameters\n    ----------')
    doc = doc[params_start_idx:]
    doc = gp_regr_doc_start + " " + doc

    pattern = re.compile(r'(\n\s+)(module .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + gp_regr_module_text + doc[end:]

    pattern = re.compile(r'(\n\s+)(criterion .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + gp_regr_criterion_text + doc[end:]

    pattern = re.compile(r'(\n\s+)(train_split .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + gp_regr_train_split_text + doc[end:]

    doc = doc + gp_likelihood_attribute_text

    return doc


class GPRegressor(_GPRegressorPredictMixin, GPBase):
    __doc__ = get_gp_regr_doc(NeuralNet.__doc__)

    def __init__(
            self,
            module,
            *args,
            likelihood=gpytorch.likelihoods.GaussianLikelihood,
            criterion=gpytorch.mlls.VariationalELBO,
            **kwargs
    ):
        super().__init__(
            module,
            *args,
            criterion=criterion,
            likelihood=likelihood,
            **kwargs
        )


gp_binary_clf_doc_start = """Gaussian Process binary classifier

    Use this for variational and approximate Gaussian process binary
    classification. This implies that the module should by a
    :class:`~gpytorch.models.ApproximateGP` module.

"""

gp_binary_clf_module_text = """

    Module : gpytorch.models.ApproximateGP (class or instance)
      The GPyTorch module; in contrast to exact GP, the return distribution does
      not need to be Gaussian.

"""

gp_binary_clf_criterion_text = """

    likelihood : gpytorch.likelihoods.BernoulliLikelihood (class or instance)
      The likelihood used for the exact GP binary classification. Usually
      doesn't need to be changed.

    criterion : gpytorch.mlls.VariationalELBO
      The objective function to learn the approximate posterior of of the GP
      binary classification.

"""


def get_gp_binary_clf_doc(doc):
    """Customizes the net docs to avoid duplication."""
    params_start_idx = doc.find('    Parameters\n    ----------')
    doc = doc[params_start_idx:]
    doc = gp_binary_clf_doc_start + " " + doc

    pattern = re.compile(r'(\n\s+)(module .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + gp_binary_clf_module_text + doc[end:]

    pattern = re.compile(r'(\n\s+)(criterion .*\n)(\s.+){1,99}')
    start, end = pattern.search(doc).span()
    doc = doc[:start] + gp_binary_clf_criterion_text + doc[end:]

    doc = doc + gp_likelihood_attribute_text

    return doc


class GPBinaryClassifier(GPBase):
    __doc__ = get_gp_binary_clf_doc(NeuralNet.__doc__)

    def __init__(
            self,
            module,
            *args,
            likelihood=gpytorch.likelihoods.BernoulliLikelihood,
            criterion=gpytorch.mlls.VariationalELBO,
            train_split=CVSplit(5, stratified=True),
            threshold=0.5,
            **kwargs
    ):
        super().__init__(
            module,
            *args,
            criterion=criterion,
            likelihood=likelihood,
            train_split=train_split,
            **kwargs
        )
        self.threshold = threshold

    @property
    def _default_callbacks(self):
        return [
            ('epoch_timer', EpochTimer()),
            ('train_loss', PassthroughScoring(
                name='train_loss',
                on_train=True,
            )),
            ('valid_loss', PassthroughScoring(
                name='valid_loss',
            )),
            # add train accuracy because by default, there is no valid split
            ('train_acc', EpochScoring(
                'accuracy',
                name='train_acc',
                lower_is_better=False,
                on_train=True,
            )),
            ('valid_acc', EpochScoring(
                'accuracy',
                name='valid_acc',
                lower_is_better=False,
            )),
            ('print_log', PrintLog()),
        ]

    @property
    def classes_(self):
        return [0, 1]

    # pylint: disable=signature-differs
    def check_data(self, X, y):
        super().check_data(X, y)
        if (not is_dataset(X)) and (get_dim(y) != 1):
            raise ValueError("The target data should be 1-dimensional.")

    def predict_proba(self, X):
        """Return probability estimates for the samples.

        If the module's forward method returns multiple outputs as a
        tuple, it is assumed that the first output contains the
        relevant information and the other values are ignored. If all
        values are relevant, consider using
        :meth:`.forward` instead.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * scipy sparse CSR matrices
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        Returns
        -------
        y_proba : numpy ndarray
          Probabilities for the samples, with the first column corresponding to
          class 0 and the second to class 1.

        """
        nonlin = self._get_predict_nonlinearity()
        y_probas = []
        for yi in self.forward_iter(X, training=False):
            posterior = yi[0] if isinstance(yi, tuple) else yi
            y_probas.append(to_numpy(nonlin(posterior.mean)))

        y_proba = np.concatenate(y_probas, 0).reshape(-1, 1)
        return np.hstack((1 - y_proba, y_proba))

    def predict(self, X):
        """Return class labels for samples in X.

        If the module's forward method returns multiple outputs as a
        tuple, it is assumed that the first output contains the
        relevant information and the other values are ignored. If all
        values are relevant, consider using
        :meth:`.forward` instead.

        Parameters
        ----------
        X : input data, compatible with skorch.dataset.Dataset
          By default, you should be able to pass:

            * numpy arrays
            * torch tensors
            * pandas DataFrame or Series
            * scipy sparse CSR matrices
            * a dictionary of the former three
            * a list/tuple of the former three
            * a Dataset

          If this doesn't work with your data, you have to pass a
          ``Dataset`` that can deal with the data.

        Returns
        -------
        y_pred : numpy ndarray
          Predicted target values for ``X``.

        """
        y_proba = self.predict_proba(X)
        return (y_proba[:, 1] > self.threshold).astype('uint8')


# BB: I could never get any reasonable results using ``SoftmaxLikelihood``. In
# fact, it always produces NaN. Probably I use it wrongly but there are no
# complete examples that I could find. I leave the commented code here for now,
# in the hopes that there is an easy fix in the future.

# class _GPClassifier(GPBase):
#     def __init__(
#             self,
#             module,
#             *args,
#             likelihood=gpytorch.likelihoods.SoftmaxLikelihood,
#             criterion=gpytorch.mlls.VariationalELBO,
#             train_split=CVSplit(5, stratified=True),
#             classes=None,
#             **kwargs
#     ):
#         super().__init__(
#             module,
#             *args,
#             criterion=criterion,
#             likelihood=likelihood,
#             train_split=train_split,
#             **kwargs
#         )
#         self.classes = classes

#     @property
#     def _default_callbacks(self):
#         return [
#             ('epoch_timer', EpochTimer()),
#             ('train_loss', PassthroughScoring(
#                 name='train_loss',
#                 on_train=True,
#             )),
#             ('valid_loss', PassthroughScoring(
#                 name='valid_loss',
#             )),
#             # add train accuracy because by default, there is no valid split
#             ('train_acc', EpochScoring(
#                 'accuracy',
#                 name='train_acc',
#                 lower_is_better=False,
#                 on_train=True,
#             )),
#             ('valid_acc', EpochScoring(
#                 'accuracy',
#                 name='valid_acc',
#                 lower_is_better=False,
#             )),
#             ('print_log', PrintLog()),
#         ]

#     @property
#     def classes_(self):
#         if self.classes is not None:
#             if not len(self.classes):
#                 raise AttributeError("{} has no attribute 'classes_'".format(
#                     self.__class__.__name__))
#             return self.classes
#         return self.classes_inferred_

#     # pylint: disable=signature-differs
#     def check_data(self, X, y):
#         if (
#                 (y is None) and
#                 (not is_dataset(X)) and
#                 (self.iterator_train is DataLoader)
#         ):
#             msg = ("No y-values are given (y=None). You must either supply a "
#                    "Dataset as X or implement your own DataLoader for "
#                    "training (and your validation) and supply it using the "
#                    "``iterator_train`` and ``iterator_valid`` parameters "
#                    "respectively.")
#             raise ValueError(msg)
#         if y is not None:
#             # pylint: disable=attribute-defined-outside-init
#             self.classes_inferred_ = np.unique(y)

#     def predict_proba(self, X):
#         """TODO"""
#         nonlin = self._get_predict_nonlinearity()
#         y_probas = []
#         for yi in self.forward_iter(X, training=False):
#             posterior = yi[0] if isinstance(yi, tuple) else yi
#             y_probas.append(to_numpy(nonlin(posterior.mean)))

#         y_proba = np.concatenate(y_probas, 0)
#         return y_proba

#     def predict(self, X):
#         """TODO
#         """
#         return self.predict_proba(X).argmax(axis=1)
