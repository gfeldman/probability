# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""GLMNet with Hessian/proximal gradient descent based optimization.

This optimizer is a variant of GLMNet which uses proximal gradient descent and a
step size dependent on the Hessian to efficiently minimize a convex loss
function with L1 and L2 regularization.  For GLMs, we approximate the Hessian
with the Fisher information matrix.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np

import tensorflow as tf
from tensorflow.python.framework import smart_cond

__all__ = [
    'fit_sparse',
    'fit_sparse_one_step',
    'soft_threshold',
]


def _is_sparse(x):
  return isinstance(x, (tf.SparseTensor, tf.SparseTensorValue))


def _reduce_ignoring_nones(fn, args):
  result = None
  for x in args:
    if x is not None:
      result = (x if result is None else fn(result, x))
  return result


def _reduce_exiting_on_none(fn, args):
  args = list(args)
  if any(x is None for x in args):
    return None
  return _reduce_ignoring_nones(fn, args)


def _add_ignoring_nones(*args):
  return _reduce_ignoring_nones(lambda a, b: a + b, args)


def _mul_ignoring_nones(*args):
  return _reduce_ignoring_nones(lambda a, b: a * b, args)


def _mul_or_none(*args):
  return _reduce_exiting_on_none(lambda a, b: a * b, args)


def _sparse_or_dense_matmul(sparse_or_dense_x, dense_y, **kwargs):
  if _is_sparse(sparse_or_dense_x):
    return tf.sparse_tensor_dense_matmul(sparse_or_dense_x, dense_y, **kwargs)
  else:
    return tf.matmul(sparse_or_dense_x, dense_y, **kwargs)


def _sparse_or_dense_matvecmul(sparse_or_dense_matrix, dense_vector, **kwargs):
  return tf.squeeze(
      _sparse_or_dense_matmul(sparse_or_dense_matrix,
                              dense_vector[..., tf.newaxis], **kwargs),
      axis=[-1])


def _sparse_or_dense_matmul_onehot(sparse_or_dense_matrix, col_index, size):
  """Returns a (dense) column of a Tensor or SparseTensor.

  Args:
    sparse_or_dense_matrix: matrix-shaped, `float` `Tensor` or `SparseTensor`.
    col_index: scalar, `int` `Tensor` representing the index of the desired
      column.
    size: scalar, `int` `Tensor` representing the number of rows in
      `sparse_or_dense_matrix`.  Used only in the sparse case, so that the
      caller can give side information about the shape of
      `sparse_or_dense_matrix`.

  Returns:
    column: vector-shaped, `float` `Tensor` with the same dtype as
      `sparse_or_dense_matrix`, representing the `col_index`th column of
      `sparse_or_dense_matrix`.
  """
  if _is_sparse(sparse_or_dense_matrix):
    # TODO(b/111924846): Implement better (ideally in a way that allows us to
    # eliminate the `size` arg, if possible).
    return tf.sparse_tensor_to_dense(
        tf.sparse_reshape(
            tf.sparse_slice(sparse_or_dense_matrix, tf.cast([0, col_index],
                                                            tf.int64),
                            tf.cast([size, 1], tf.int64)),
            [size]))
  else:
    return tf.gather(sparse_or_dense_matrix, col_index, axis=-1)


def _sparse_or_dense_inner_square(sparse_or_dense_vector):
  if _is_sparse(sparse_or_dense_vector):
    return tf.reduce_sum(sparse_or_dense_vector.values**2)
  else:
    return tf.reduce_sum(sparse_or_dense_vector**2)


def _one_hot_like(x, indices, on_value=None):
  output_dtype = x.dtype.base_dtype
  if (x.shape.ndims is None or x.shape[-1].value is None):
    depth = tf.shape(x)[-1]
  else:
    depth = x.shape[-1].value
  if on_value is not None:
    on_value = tf.cast(on_value, output_dtype)
  return tf.one_hot(
      indices, depth=depth, on_value=on_value, dtype=output_dtype)


def soft_threshold(x, threshold, name=None):
  """Soft Thresholding operator.

  This operator is defined by the equations

  ```none
                                { x[i] - gamma,  x[i] >   gamma
  SoftThreshold(x, gamma)[i] =  { 0,             x[i] ==  gamma
                                { x[i] + gamma,  x[i] <  -gamma
  ```

  In the context of proximal gradient methods, we have

  ```none
  SoftThreshold(x, gamma) = prox_{gamma L1}(x)
  ```

  where `prox` is the proximity operator.  Thus the soft thresholding operator
  is used in proximal gradient descent for optimizing a smooth function with
  (non-smooth) L1 regularization, as outlined below.

  The proximity operator is defined as:

  ```none
  prox_r(x) = argmin{ r(z) + 0.5 ||x - z||_2**2 : z },
  ```

  where `r` is a (weakly) convex function, not necessarily differentiable.
  Because the L2 norm is strictly convex, the above argmin is unique.

  One important application of the proximity operator is as follows.  Let `L` be
  a convex and differentiable function with Lipschitz-continuous gradient.  Let
  `R` be a convex lower semicontinuous function which is possibly
  nondifferentiable.  Let `gamma` be an arbitrary positive real.  Then

  ```none
  x_star = argmin{ L(x) + R(x) : x }
  ```

  if and only if the fixed-point equation is satisfied:

  ```none
  x_star = prox_{gamma R}(x_star - gamma grad L(x_star))
  ```

  Proximal gradient descent thus typically consists of choosing an initial value
  `x^{(0)}` and repeatedly applying the update

  ```none
  x^{(k+1)} = prox_{gamma^{(k)} R}(x^{(k)} - gamma^{(k)} grad L(x^{(k)}))
  ```

  where `gamma` is allowed to vary from iteration to iteration.  Specializing to
  the case where `R(x) = ||x||_1`, we minimize `L(x) + ||x||_1` by repeatedly
  applying the update

  ```
  x^{(k+1)} = SoftThreshold(x - gamma grad L(x^{(k)}), gamma)
  ```

  (This idea can also be extended to second-order approximations, although the
  multivariate case does not have a known closed form like above.)

  Args:
    x: `float` `Tensor` representing the input to the SoftThreshold function.
    threshold: nonnegative scalar, `float` `Tensor` representing the radius of
      the interval on which each coordinate of SoftThreshold takes the value
      zero.  Denoted `gamma` above.
    name: Python string indicating the name of the TensorFlow operation.
      Default name is `"soft_threshold"`.

  Returns:
    softthreshold: `float` `Tensor` with the same shape and dtype as `x`,
      representing the value of the SoftThreshold function.

  #### References

  [1]: Yu, Yao-Liang. The Proximity Operator.
       https://www.cs.cmu.edu/~suvrit/teach/yaoliang_proximity.pdf

  [2]: Wikipedia Contributors. Proximal gradient methods for learning.
       _Wikipedia, The Free Encyclopedia_, 2018.
       https://en.wikipedia.org/wiki/Proximal_gradient_methods_for_learning

  """
  # https://math.stackexchange.com/questions/471339/derivation-of-soft-thresholding-operator
  with tf.name_scope(name, 'soft_threshold', [x, threshold]):
    x = tf.convert_to_tensor(x, name='x')
    threshold = tf.convert_to_tensor(threshold, name='threshold')
    return tf.sign(x) * tf.maximum(tf.abs(x) - threshold, 0.)


def _grad_neg_log_likelihood_and_fim(model_matrix, linear_response, response,
                                     model):
  """Computes the neg-log-likelihood gradient and Fisher information for a GLM.

  Note that Fisher information is related to the Hessian of the log-likelihood
  by the equation

  ```none
  FisherInfo = E[Hessian with respect to model_coefficients of -LogLikelihood(
      Y | model_matrix, model_coefficients)]
  ```

  where `LogLikelihood` is the log-likelihood of a generalized linear model
  parameterized by `model_matrix` and `model_coefficients`, and the expectation
  is taken over Y, distributed according to the same GLM with the same parameter
  values.

  Args:
    model_matrix: matrix-shaped, `float` `Tensor` or `SparseTensor` where each
      row represents a sample's features.  Has shape `[N, n]` where `N` is the
      number of data samples and `n` is the number of features per sample.
    linear_response: vector-shaped `Tensor` with the same dtype as
      `model_matrix`, equal to `model_matix @ model_coefficients` where
      `model_coefficients` are the coefficients of the linear component of the
      GLM.
    response: vector-shaped `Tensor` with the same dtype as `model_matrix` where
      each element represents a sample's observed response (to the corresponding
      row of features).
    model: `tfp.glm.ExponentialFamily`-like instance, which specifies the link
      function and distribution of the GLM, and thus characterizes the negative
      log-likelihood. Must have sufficient statistic equal to the response, that
      is, `T(y) = y`.

  Returns:
    grad_neg_log_likelihood: vector-shaped `Tensor` with the same shape and
      dtype as a single row of `model_matrix`, representing the gradient of the
      negative log likelihood of `response` given linear response
      `linear_response`.
    fim_middle: vector-shaped `Tensor` with the same shape and dtype as a single
      column of `model_matrix`, satisfying the equation
      `Fisher information =
      Transpose(model_matrix)
      @ diag(fim_middle)
      @ model_matrix`.
  """
  # TODO(b/111926503): Determine whether there are some practical cases where it
  # is computationally favorable to compute the full FIM.
  mean, variance, grad_mean = model(linear_response)

  is_valid = (
      tf.is_finite(grad_mean) & tf.not_equal(grad_mean, 0.) &
      tf.is_finite(variance) & (variance > 0.))

  def _mask_if_invalid(x, mask):
    mask = tf.fill(tf.shape(x), value=np.array(mask, x.dtype.as_numpy_dtype))
    return tf.where(is_valid, x, mask)

  # TODO(b/111923449): Link to derivation once it's available.
  v = (response - mean) * _mask_if_invalid(grad_mean, 1) / _mask_if_invalid(
      variance, np.inf)
  grad_log_likelihood = _sparse_or_dense_matvecmul(
      model_matrix, v, adjoint_a=True)
  fim_middle = _mask_if_invalid(grad_mean, 0.)**2 / _mask_if_invalid(
      variance, np.inf)
  return -grad_log_likelihood, fim_middle


def minimize_sparse_one_step(gradient_unregularized_loss,
                             hessian_unregularized_loss_outer,
                             hessian_unregularized_loss_middle,
                             x_start,
                             tolerance,
                             l1_regularizer,
                             l2_regularizer=None,
                             maximum_full_sweeps=1,
                             learning_rate=None,
                             x_update_var=None,
                             name=None):
  """One step of (the outer loop of) GLMNet's minimization algorithm.

  This function returns a new value of `x`, equal to `x_start + x_update`.  The
  increment `x_update in R^n` is computed by a coordinate descent method, that
  is, by a loop in which each iteration updates exactly one coordinate of
  `x_update`.  (Some updates may leave the value of the coordinate unchanged.)

  The particular update method used is to apply an L1-based proximity operator,
  "soft threshold", whose fixed point `x_update_fix` is the desired minimum

  ```none
  x_update_fix = argmin{
      Loss(x_start + x_update')
        + l1_regularizer * ||x_start + x_update'||_1
        + l2_regularizer * ||x_start + x_update'||_2**2
      : x_update' }
  ```

  where in each iteration `x_update'` is constrained to have at most one nonzero
  coordinate.

  This update method preserves sparsity, i.e., tends to find sparse solutions if
  `x_start` is sparse.  Additionally, the choice of step size is based on
  curvature (Hessian), which significantly speeds up convergence.

  This algorithm assumes that `Loss` is convex, at least in a region surrounding
  the optimum.  (If `l2_regularizer > 0`, then only weak convexity is needed.)

  Note that this function does not support batched inputs.

  Args:
    gradient_unregularized_loss: `Tensor` with the same shape and dtype as
      `x_start` representing the gradient, evaluated at `x_start`, of the
      unregularized loss function (denoted `Loss` above).  (In all current use
      cases, `Loss` is the negative log likelihood.)
    hessian_unregularized_loss_outer: `Tensor` or `SparseTensor` having the same
      dtype as `x_start`, and shape `[N, n]` where `x_start` has shape `[n]`,
      satisfying the property
      `Transpose(hessian_unregularized_loss_outer)
      @ diag(hessian_unregularized_loss_middle)
      @ hessian_unregularized_loss_inner
      = (approximation of) Hessian matrix of Loss, evaluated at x_start`.
    hessian_unregularized_loss_middle: vector-shaped `Tensor` having the same
      dtype as `x_start`, and shape `[N]` where
      `hessian_unregularized_loss_outer` has shape `[N, n]`, satisfying the
      property
      `Transpose(hessian_unregularized_loss_outer)
      @ diag(hessian_unregularized_loss_middle)
      @ hessian_unregularized_loss_inner
      = (approximation of) Hessian matrix of Loss, evaluated at x_start`.
    x_start: vector-shaped, `float` `Tensor` representing the current value of
      the argument to the Loss function.
    tolerance: scalar, `float` `Tensor` representing the convergence threshold.
      The optimization step will terminate early, returning its current value of
      `x_start + x_update`, once the following condition is met:
      `||x_update_end - x_update_start||_2 / (1 + ||x_start||_2)
       < sqrt(tolerance)`,
      where `x_update_end` is the value of `x_update` at the end of a sweep and
      `x_update_start` is the value of `x_update` at the beginning of that
      sweep.
    l1_regularizer: scalar, `float` `Tensor` representing the weight of the L1
      regularization term (see equation above).  If L1 regularization is not
      required, then `tfp.glm.fit_one_step` is preferable.
    l2_regularizer: scalar, `float` `Tensor` representing the weight of the L2
      regularization term (see equation above).
      Default value: `None` (i.e., no L2 regularization).
    maximum_full_sweeps: Python integer specifying maximum number of sweeps to
      run.  A "sweep" consists of an iteration of coordinate descent on each
      coordinate. After this many sweeps, the algorithm will terminate even if
      convergence has not been reached.
      Default value: `1`.
    learning_rate: scalar, `float` `Tensor` representing a multiplicative factor
      used to dampen the proximal gradient descent steps.
      Default value: `None` (i.e., factor is conceptually `1`).
    x_update_var: `Variable` with the same shape and dtype as `x_start`.  Used
      to store the current value of `x_update`.
      Default value: `None` (i.e., a new `Variable` will be created).
    name: Python string representing the name of the TensorFlow operation.
      The default name is `"minimize_sparse_one_step"`.

  Returns:
    x: `Tensor` having the same shape and dtype as `x_start`, representing the
      updated value of `x`, that is, `x_start + x_update`.
    is_converged: scalar, `bool` `Tensor` indicating whether convergence
      occurred within the specified number of sweeps.
    iter: scalar, `int` `Tensor` representing the actual number of coordinate
      updates made (before achieving convergence).  Since each sweep consists of
      `tf.size(x_start)` iterations, the maximum number of updates is
      `maximum_full_sweeps * tf.size(x_start)`.

  #### References

  [1]: Jerome Friedman, Trevor Hastie and Rob Tibshirani. Regularization Paths
       for Generalized Linear Models via Coordinate Descent. _Journal of
       Statistical Software_, 33(1), 2010.
       https://www.jstatsoft.org/article/view/v033i01/v33i01.pdf

  [2]: Guo-Xun Yuan, Chia-Hua Ho and Chih-Jen Lin. An Improved GLMNET for
       L1-regularized Logistic Regression. _Journal of Machine Learning
       Research_, 13, 2012.
       http://www.jmlr.org/papers/volume13/yuan12a/yuan12a.pdf
  """
  graph_deps = [
      gradient_unregularized_loss,
      hessian_unregularized_loss_outer,
      hessian_unregularized_loss_middle,
      x_start,
      l1_regularizer,
      l2_regularizer,
      maximum_full_sweeps,
      tolerance,
      learning_rate,
      x_update_var,
  ]
  with tf.name_scope(name, 'minimize_sparse_one_step', graph_deps):
    if (x_start.shape.ndims is None or x_start.shape[-1].value is None):
      dims = tf.shape(x_start)[-1]
    else:
      dims = np.array(x_start.shape[-1].value, np.int32)

    if (hessian_unregularized_loss_outer.get_shape().ndims is None or
        hessian_unregularized_loss_outer.get_shape()[0].value is None):
      num_samples = tf.shape(hessian_unregularized_loss_outer)[0]
    else:
      num_samples = np.array(
          hessian_unregularized_loss_outer.get_shape()[0].value, np.int32)

    # Hint vector shape for dynamically shaped vector arguments
    if gradient_unregularized_loss.shape.ndims is None:
      gradient_unregularized_loss.set_shape([None])
    if hessian_unregularized_loss_middle.shape.ndims is None:
      hessian_unregularized_loss_middle.set_shape([None])

    # Hint matrix shape for dynamically shaped matrix arguments
    if (not _is_sparse(hessian_unregularized_loss_outer) and
        hessian_unregularized_loss_outer.shape.ndims is None):
      hessian_unregularized_loss_outer.set_shape([None, None])

    if x_update_var is None:
      x_update_var = tf.get_variable(
          name='x_update_var',
          initializer=tf.zeros([dims], dtype=x_start.dtype.as_numpy_dtype),
          trainable=False,
          use_resource=True)

    def _hessian_diag_elt_with_l2(coord):  # pylint: disable=missing-docstring
      # Returns the (coord, coord) entry of
      #
      #   Hessian(UnregularizedLoss(x) + l2_regularizer * ||x||_2**2)
      #
      # evaluated at x = x_start.
      unregularized_component = (
          hessian_unregularized_loss_middle[coord] *
          _sparse_or_dense_inner_square(
              _sparse_or_dense_matmul_onehot(hessian_unregularized_loss_outer,
                                             coord, num_samples)))
      l2_component = _mul_or_none(2., l2_regularizer)
      return _add_ignoring_nones(unregularized_component, l2_component)

    grad_loss_with_l2 = _add_ignoring_nones(
        gradient_unregularized_loss,
        _mul_or_none(2., l2_regularizer, x_start))

    # We define `x_update_diff_norm_sq_convergence_threshold` such that the
    # convergence condition
    #     ||x_update_end - x_update_start||_2 / (1 + ||x_start||_2)
    #     < sqrt(tolerance)
    # is equivalent to
    #     ||x_update_end - x_update_start||_2
    #     < x_update_diff_norm_sq_convergence_threshold.
    x_update_diff_norm_sq_convergence_threshold = (
        tolerance * (1. + tf.norm(x_start, ord=2))**2.)

    def _loop_cond(iter_, x_update_diff_norm_sq, x_update,
                   hess_matmul_x_update):
      del x_update
      del hess_matmul_x_update
      sweep_complete = (iter_ > 0) & tf.equal(iter_ % dims, 0)
      small_delta = (
          x_update_diff_norm_sq < x_update_diff_norm_sq_convergence_threshold)
      converged = sweep_complete & small_delta
      allowed_more_iterations = iter_ < maximum_full_sweeps * dims
      return allowed_more_iterations & ~converged

    def _loop_body(iter_, x_update_diff_norm_sq, x_update,  # pylint: disable=missing-docstring
                   hess_matmul_x_update):
      # Inner loop of GLMNet's minimizer.
      #
      # This loop updates a single coordinate of x_update.  Ideally, an
      # iteration of this loop would set
      #
      #   x_update[j] += argmin{ LocalLoss(x_update + z*e_j) : z in R }
      #
      # where
      #
      #   LocalLoss(x_update')
      #     = LocalLossSmoothComponent(x_update')
      #         + l1_regularizer * (||x_start + x_update'||_1 -
      #                             ||x_start + x_update||_1)
      #    := (UnregularizedLoss(x_start + x_update') -
      #        UnregularizedLoss(x_start + x_update)
      #         + l2_regularizer * (||x_start + x_update'||_2**2 -
      #                             ||x_start + x_update||_2**2)
      #         + l1_regularizer * (||x_start + x_update'||_1 -
      #                             ||x_start + x_update||_1)
      #
      # In this algorithm approximate the above argmin using (univariate)
      # proximal gradient descent:
      #
      # (*)  x_update[j] = prox_{t * l1_regularizer * L1}(
      #                 x_update[j] -
      #                 t * d/dz|z=0 UnivariateLocalLossSmoothComponent(z))
      #
      # where
      #
      #   UnivariateLocalLossSmoothComponent(z)
      #       := LocalLossSmoothComponent(x_update + z*e_j)
      #
      # and we approximate
      #
      #       d/dz UnivariateLocalLossSmoothComponent(z)
      #     = grad LocalLossSmoothComponent(x_update))[j]
      #    ~= (grad LossSmoothComponent(x_start)
      #         + x_update matmul HessianOfLossSmoothComponent(x_start))[j].
      #
      # To choose the parameter t, we squint and pretend that the inner term of
      # (*) is a Newton update as if we were using Newton's method to minimize
      # UnivariateLocalLossSmoothComponent.  That is, we choose t such that
      #
      #   -t * d/dz ULLSC = -learning_rate * (d/dz ULLSC) / (d^2/dz^2 ULLSC)
      #
      # at z=0.  Hence
      #
      #   t = learning_rate / (d^2/dz^2|z=0 ULLSC)
      #     = learning_rate / HessianOfLossSmoothComponent(
      #                           x_start + x_update)[j,j]
      #    ~= learning_rate / HessianOfLossSmoothComponent(
      #                           x_start)[j,j]
      #
      # The above approximation is equivalent to assuming that
      # HessianOfUnregularizedLoss is constant, i.e., ignoring third-order
      # effects.
      #
      # Note that because LossSmoothComponent is (assumed to be) convex, t is
      # positive.

      # In above notation, coord = j.
      coord = iter_ % dims
      # x_update_diff_norm_sq := ||x_update_end - x_update_start||_2**2,
      # computed incrementally, where x_update_end and x_update_start are as
      # defined in the convergence criteria.  Accordingly, we reset
      # x_update_diff_norm_sq to zero at the beginning of each sweep.
      x_update_diff_norm_sq = tf.where(
          tf.equal(coord, 0), tf.zeros_like(x_update_diff_norm_sq),
          x_update_diff_norm_sq)

      w_old = x_start[coord] + x_update[coord]
      # This is the coordinatwise Newton update if no L1 regularization.
      # In above notation, newton_step = -t * (approximation of d/dz|z=0 ULLSC).
      second_deriv = _hessian_diag_elt_with_l2(coord)
      newton_step = -_mul_ignoring_nones(  # pylint: disable=invalid-unary-operand-type
          learning_rate,
          grad_loss_with_l2[coord] + hess_matmul_x_update[coord]) / second_deriv
      # Applying the soft-threshold operator accounts for L1 regularization.
      # In above notation, delta =
      #     prox_{t*l1_regularizer*L1}(w_old + newton_step) - w_old.
      delta = (
          soft_threshold(
              w_old + newton_step,
              _mul_ignoring_nones(learning_rate, l1_regularizer) / second_deriv)
          - w_old)

      def _do_update(x_update_diff_norm_sq, x_update, hess_matmul_x_update):  # pylint: disable=missing-docstring
        del x_update
        hessian_column_with_l2 = _sparse_or_dense_matvecmul(
            hessian_unregularized_loss_outer,
            hessian_unregularized_loss_middle * _sparse_or_dense_matmul_onehot(
                hessian_unregularized_loss_outer, coord, num_samples),
            adjoint_a=True)
        if l2_regularizer is not None:
          hessian_column_with_l2 += _one_hot_like(
              hessian_column_with_l2,
              coord,
              on_value=2. * l2_regularizer)
        changed_x_update_var = tf.scatter_update(x_update_var, [coord],
                                                 [x_update_var[coord] + delta])
        with tf.control_dependencies([changed_x_update_var]):
          x_update_diff_norm_sq_ = x_update_diff_norm_sq + delta**2.
          hess_matmul_x_update_ = (
              hess_matmul_x_update + delta * hessian_column_with_l2)
          return [
              x_update_diff_norm_sq_, changed_x_update_var,
              hess_matmul_x_update_
          ]

      inputs_to_update = [x_update_diff_norm_sq, x_update, hess_matmul_x_update]
      return [iter_ + 1] + smart_cond.smart_cond(
          # Note on why checking delta (a difference of floats) for equality to
          # zero is ok:
          #
          # First of all, x - x == 0 in floating point -- see
          # https://stackoverflow.com/a/2686671
          #
          # Delta will conceptually equal zero when one of the following holds:
          # (i)   |w_old + newton_step| <= threshold and w_old == 0
          # (ii)  |w_old + newton_step| > threshold and
          #       w_old + newton_step - sign(w_old + newton_step) * threshold
          #          == w_old
          #
          # In case (i) comparing delta to zero is fine.
          #
          # In case (ii), newton_step conceptually equals
          #     sign(w_old + newton_step) * threshold.
          # Also remember
          #     threshold = -newton_step / (approximation of d/dz|z=0 ULLSC).
          # So (i) happens when
          #     (approximation of d/dz|z=0 ULLSC) == -sign(w_old + newton_step).
          # If we did not require LossSmoothComponent to be strictly convex,
          # then this could actually happen a non-negligible amount of the time,
          # e.g. if the loss function is piecewise linear and one of the pieces
          # has slope 1.  But since LossSmoothComponent is strictly convex, (i)
          # should not systematically happen.
          tf.equal(delta, 0.),
          lambda: inputs_to_update,
          lambda: _do_update(*inputs_to_update)
      )

    if (x_start.shape.ndims is not None and
        x_start.shape[-1].value is not None):
      dims = x_start.shape[-1].value
    else:
      dims = tf.shape(x_start)[-1]

    iter_, x_update_diff_norm_sq, x_update, _ = tf.while_loop(
        cond=_loop_cond,
        body=_loop_body,
        loop_vars=[
            tf.zeros([], np.int32, name='iter'),
            tf.zeros(
                [], x_update_var.dtype.base_dtype,
                name='x_update_diff_norm_sq'),
            tf.assign(
                x_update_var, tf.zeros_like(x_update_var), name='x_update'),
            tf.zeros_like(x_update_var, name='hess_matmul_x_update'),
        ])

    converged = (
        x_update_diff_norm_sq < x_update_diff_norm_sq_convergence_threshold)
    return x_start + x_update, converged, iter_ / dims


def minimize_sparse(grad_and_hessian_loss_fn,
                    x_start,
                    tolerance,
                    l1_regularizer,
                    l2_regularizer=None,
                    maximum_iterations=1,
                    maximum_full_sweeps_per_iteration=1,
                    learning_rate=None,
                    x_update_var=None,
                    name=None):
  """Minimize using Hessian-informed proximal gradient descent.

  This function solves the regularized minimization problem

  ```none
  argmin{ Loss(x)
            + l1_regularizer * ||x||_1
            + l2_regularizer * ||x||_2**2
          : x in R^n }
  ```

  where `Loss` is a convex C^2 function (typically, `Loss` is the negative log
  likelihood of a model and `x` is a vector of model coefficients).  The `Loss`
  function does not need to be supplied directly, but this optimizer does need a
  way to compute the gradient and Hessian of the Loss function at a given value
  of `x`.  The gradient and Hessian are often computationally expensive, and
  this optimizer calls them relatively few times compared with other algorithms.

  Note that this function does not support batched inputs.

  Args:
    grad_and_hessian_loss_fn: callable that takes as input a `Tensor` of the
      same shape and dtype as `x_start` and returns the triple
      `(gradient_unregularized_loss, hessian_unregularized_loss_outer,
      hessian_unregularized_loss_middle)` as defined in the argument spec of
      `minimize_sparse_one_step`.
    x_start: vector-shaped, `float` `Tensor` representing the initial value of
      the argument to the `Loss` function.
    tolerance: scalar, `float` `Tensor` representing the tolerance for each
      optiization step; see the `tolerance` argument of
      `minimize_sparse_one_step`.
    l1_regularizer: scalar, `float` `Tensor` representing the weight of the L1
      regularization term (see equation above).
    l2_regularizer: scalar, `float` `Tensor` representing the weight of the L2
      regularization term (see equation above).
      Default value: `None` (i.e., no L2 regularization).
    maximum_iterations: Python integer specifying the maximum number of
      iterations of the outer loop of GLMNet.  After this many iterations of the
      outer loop, the algorithm will terminate even if the return value
      `optimal_x` has not converged.
      Default value: `1`.
    maximum_full_sweeps_per_iteration: Python integer specifying the Maximum
      number of sweeps allowed in each iteration of the outer loop of GLMNet.
      Passed as the `maximum_full_sweeps` argument to
      `minimize_sparse_one_step`.
      Default value: `1`.
    learning_rate: scalar, `float` `Tensor` representing a multiplicative factor
      used to dampen the proximal gradient descent steps.
      Default value: `None` (i.e., factor is conceptually `1`).
    x_update_var: `Variable` with the same shape and dtype as `x_start`.  Used
      to store the current value of `x_update`.
      Default value: `None` (i.e., a new `Variable` will be created).
    name: Python string representing the name of the TensorFlow operation.
      The default name is `"minimize_sparse"`.

  Returns:
    x: `Tensor` of the same shape and dtype as `x_start`, representing the
      computed value of `x` which minimizes `Loss(x)`.
    is_converged: scalar, `bool` `Tensor` indicating whether the minimization
      procedure converged within the specified number of iterations.  Here
      convergence means that an iteration of the inner loop
      (`minimize_sparse_one_step`) returns `True` for its `is_converged` output
      value.
    iter: scalar, `int` `Tensor` indicating the actual number of iterations of
      the outer loop of GLMNet completed (i.e., number of calls to
      `minimize_sparse_one_step` before achieving convergence).

  #### References

  [1]: Jerome Friedman, Trevor Hastie and Rob Tibshirani. Regularization Paths
       for Generalized Linear Models via Coordinate Descent. _Journal of
       Statistical Software_, 33(1), 2010.
       https://www.jstatsoft.org/article/view/v033i01/v33i01.pdf

  [2]: Guo-Xun Yuan, Chia-Hua Ho and Chih-Jen Lin. An Improved GLMNET for
       L1-regularized Logistic Regression. _Journal of Machine Learning
       Research_, 13, 2012.
       http://www.jmlr.org/papers/volume13/yuan12a/yuan12a.pdf
  """
  # TODO(b/111926449): Do we want to require x_update_var to be a
  # ResourceVariable if the user is in eager mode?  Maybe depends on whether the
  # error they get would be informative enough.
  graph_deps = [
      x_start,
      l1_regularizer,
      l2_regularizer,
      maximum_iterations,
      maximum_full_sweeps_per_iteration,
      tolerance,
      learning_rate,
      x_update_var,
  ],
  with tf.name_scope(name, 'minimize_sparse', graph_deps):

    if x_update_var is None:
      dims = np.array(x_start.shape[-1].value, np.int32)
      x_update_var = tf.get_variable(
          name='x_update_var',
          initializer=np.zeros([dims], dtype=x_start.dtype.as_numpy_dtype),
          trainable=False,
          use_resource=True)

    def _loop_cond(x_start, converged, iter_):
      del x_start
      return tf.logical_and(iter_ < maximum_iterations,
                            tf.logical_not(converged))

    def _loop_body(x_start, converged, iter_):  # pylint: disable=missing-docstring
      g, h_outer, h_middle = grad_and_hessian_loss_fn(x_start)
      x_start, converged, _ = minimize_sparse_one_step(
          gradient_unregularized_loss=g,
          hessian_unregularized_loss_outer=h_outer,
          hessian_unregularized_loss_middle=h_middle,
          x_start=x_start,
          l1_regularizer=l1_regularizer,
          l2_regularizer=l2_regularizer,
          maximum_full_sweeps=maximum_full_sweeps_per_iteration,
          tolerance=tolerance,
          x_update_var=x_update_var,
          learning_rate=learning_rate)
      return x_start, converged, iter_ + 1

    return tf.while_loop(
        cond=_loop_cond,
        body=_loop_body,
        loop_vars=[
            x_start,
            tf.zeros([], np.bool, name='converged'),
            tf.zeros([], np.int32, name='iter'),
        ])


def fit_sparse_one_step(model_matrix,
                        response,
                        model,
                        model_coefficients_start,
                        tolerance,
                        l1_regularizer,
                        l2_regularizer=None,
                        maximum_full_sweeps=None,
                        learning_rate=None,
                        model_coefficients_update_var=None,
                        name=None):
  """One step of (the outer loop of) GLMNet's fitting algorithm.

  This function returns a new value of `model_coefficients`, equal to
  `model_coefficients_start + model_coefficients_update`.  The increment
  `model_coefficients_update in R^n` is computed by a coordinate descent method,
  that is, by a loop in which each iteration updates exactly one coordinate of
  `model_coefficients_update`.  (Some updates may leave the value of the
  coordinate unchanged.)

  The particular update method used is to apply an L1-based proximity operator,
  "soft threshold", whose fixed point `model_coefficients_update^*` is the
  desired minimum

  ```none
  model_coefficients_update^* = argmin{
      -LogLikelihood(model_coefficients_start + model_coefficients_update')
        + l1_regularizer *
            ||model_coefficients_start + model_coefficients_update'||_1
        + l2_regularizer *
            ||model_coefficients_start + model_coefficients_update'||_2**2
      : model_coefficients_update' }
  ```

  where in each iteration `model_coefficients_update'` has at most one nonzero
  coordinate.

  This update method preserves sparsity, i.e., tends to find sparse solutions if
  `model_coefficients_start` is sparse.  Additionally, the choice of step size
  is based on curvature (Fisher information matrix), which significantly speeds
  up convergence.

  Note that this function does not support batched inputs.

  Args:
    model_matrix: matrix-shaped, `float` `Tensor` or `SparseTensor` where each
      row represents a sample's features.  Has shape `[N, n]` where `N` is the
      number of data samples and `n` is the number of features per sample.
    response: vector-shaped `Tensor` with the same dtype as `model_matrix` where
      each element represents a sample's observed response (to the corresponding
      row of features).
    model: `tfp.glm.ExponentialFamily`-like instance, which specifies the link
      function and distribution of the GLM, and thus characterizes the negative
      log-likelihood which will be minimized. Must have sufficient statistic
      equal to the response, that is, `T(y) = y`.
    model_coefficients_start: vector-shaped, `float` `Tensor` with the same
      dtype as `model_matrix`, representing the initial values of the
      coefficients for the GLM regression.  Has shape `[n]` where `model_matrix`
      has shape `[N, n]`.
    tolerance: scalar, `float` `Tensor` representing the convergence threshold.
      The optimization step will terminate early, returning its current value of
      `model_coefficients_start + model_coefficients_update`, once the following
      condition is met:
      `||model_coefficients_update_end - model_coefficients_update_start||_2
         / (1 + ||model_coefficients_start||_2)
       < sqrt(tolerance)`,
      where `model_coefficients_update_end` is the value of
      `model_coefficients_update` at the end of a sweep and
      `model_coefficients_update_start` is the value of
      `model_coefficients_update` at the beginning of that sweep.
    l1_regularizer: scalar, `float` `Tensor` representing the weight of the L1
      regularization term (see equation above).
    l2_regularizer: scalar, `float` `Tensor` representing the weight of the L2
      regularization term (see equation above).
      Default value: `None` (i.e., no L2 regularization).
    maximum_full_sweeps: Python integer specifying maximum number of sweeps to
      run.  A "sweep" consists of an iteration of coordinate descent on each
      coordinate. After this many sweeps, the algorithm will terminate even if
      convergence has not been reached.
      Default value: `1`.
    learning_rate: scalar, `float` `Tensor` representing a multiplicative factor
      used to dampen the proximal gradient descent steps.
      Default value: `None` (i.e., factor is conceptually `1`).
    model_coefficients_update_var: `Variable` with the same shape and dtype as
      `model_coefficients_start`.  Used to store the current value of
      `model_coefficients_update`.
      Default value: `None` (i.e., a new `Variable` will be created).
    name: Python string representing the name of the TensorFlow operation.
      The default name is `"fit_sparse_one_step"`.

  Returns:
    model_coefficients: `Tensor` having the same shape and dtype as
      `model_coefficients_start`, representing the updated value of
      `model_coefficients`, that is, `model_coefficients_start +
      model_coefficients_update`.
    is_converged: scalar, `bool` `Tensor` indicating whether convergence
      occurred within the specified number of sweeps.
    iter: scalar, `int` `Tensor` representing the actual number of coordinate
      updates made (before achieving convergence).  Since each sweep consists of
      `tf.size(model_coefficients_start)` iterations, the maximum number of
      updates is `maximum_full_sweeps * tf.size(model_coefficients_start)`.
  """
  graph_deps = [
      model_matrix,
      response,
      model_coefficients_start,
      l1_regularizer,
      l2_regularizer,
      maximum_full_sweeps,
      tolerance,
      learning_rate,
      model_coefficients_update_var,
  ]
  with tf.name_scope(name, 'fit_sparse_one_step', graph_deps):
    predicted_linear_response = _sparse_or_dense_matvecmul(
        model_matrix, model_coefficients_start)
    g, h_middle = _grad_neg_log_likelihood_and_fim(
        model_matrix, predicted_linear_response, response, model)

    return minimize_sparse_one_step(
        gradient_unregularized_loss=g,
        hessian_unregularized_loss_outer=model_matrix,
        hessian_unregularized_loss_middle=h_middle,
        x_start=model_coefficients_start,
        l1_regularizer=l1_regularizer,
        l2_regularizer=l2_regularizer,
        maximum_full_sweeps=maximum_full_sweeps,
        tolerance=tolerance,
        learning_rate=learning_rate,
        x_update_var=model_coefficients_update_var,
        name=name)


def fit_sparse(model_matrix,
               response,
               model,
               model_coefficients_start,
               tolerance,
               l1_regularizer,
               l2_regularizer=None,
               maximum_iterations=None,
               maximum_full_sweeps_per_iteration=1,
               learning_rate=None,
               model_coefficients_update_var=None,
               name=None):
  """Fits a GLM using GLMNet with FIM-informed proximal gradient descent.

  This function uses a L1- and L2-regularized, second-order quasi-Newton method
  to find maximum-likelihood parameters for the given model and observed data.
  The second-order approximations use negative Fisher information in place of
  the Hessian, that is,

  ```none
  FisherInfo = E_Y[Hessian with respect to model_coefficients of -LogLikelihood(
      Y | model_matrix, current value of model_coefficients)]
  ```

  For large, sparse data sets, `model_matrix` should be supplied as a
  `SparseTensor`.

  Args:
    model_matrix: matrix-shaped, `float` `Tensor` or `SparseTensor` where each
      row represents a sample's features.  Has shape `[N, n]` where `N` is the
      number of data samples and `n` is the number of features per sample.
    response: vector-shaped `Tensor` with the same dtype as `model_matrix` where
      each element represents a sample's observed response (to the corresponding
      row of features).
    model: `tfp.glm.ExponentialFamily`-like instance, which specifies the link
      function and distribution of the GLM, and thus characterizes the negative
      log-likelihood which will be minimized. Must have sufficient statistic
      equal to the response, that is, `T(y) = y`.
    model_coefficients_start: vector-shaped, `float` `Tensor` with the same
      dtype as `model_matrix`, representing the initial values of the
      coefficients for the GLM regression.  Has shape `[n]` where `model_matrix`
      has shape `[N, n]`.
    tolerance: scalar, `float` `Tensor` representing the tolerance for each
      optiization step; see the `tolerance` argument of `fit_sparse_one_step`.
    l1_regularizer: scalar, `float` `Tensor` representing the weight of the L1
      regularization term.
    l2_regularizer: scalar, `float` `Tensor` representing the weight of the L2
      regularization term.
      Default value: `None` (i.e., no L2 regularization).
    maximum_iterations: Python integer specifying maximum number of iterations
      of the outer loop of GLMNet (i.e., maximum number of calls to
      `fit_sparse_one_step`).  After this many iterations of the outer loop, the
      algorithm will terminate even if the return value `model_coefficients` has
      not converged.
      Default value: `1`.
    maximum_full_sweeps_per_iteration: Python integer specifying the maximum
      number of coordinate descent sweeps allowed in each iteration.
      Default value: `1`.
    learning_rate: scalar, `float` `Tensor` representing a multiplicative factor
      used to dampen the proximal gradient descent steps.
      Default value: `None` (i.e., factor is conceptually `1`).
    model_coefficients_update_var: `Variable` with the same shape and dtype as
      `model_coefficients_start`.  Used to store the current value of
      `model_coefficients_update`.
      Default value: `None` (i.e., a new `Variable` will be created).
    name: Python string representing the name of the TensorFlow operation.
      The default name is `"fit_sparse"`.

  Note that this function does not support batched inputs.

  Returns:
    model_coefficients: `Tensor` of the same shape and dtype as
      `model_coefficients_start`, representing the computed model coefficients
      which minimize the regularized negative log-likelihood.
    is_converged: scalar, `bool` `Tensor` indicating whether the minimization
      procedure converged within the specified number of iterations.  Here
      convergence means that an iteration of the inner loop
      (`fit_sparse_one_step`) returns `True` for its `is_converged` output
      value.
    iter: scalar, `int` `Tensor` indicating the actual number of iterations of
      the outer loop of GLMNet completed (i.e., number of calls to
      `fit_sparse_one_step` before achieving convergence).

  #### References

  [1]: Jerome Friedman, Trevor Hastie and Rob Tibshirani. Regularization Paths
       for Generalized Linear Models via Coordinate Descent. _Journal of
       Statistical Software_, 33(1), 2010.
       https://www.jstatsoft.org/article/view/v033i01/v33i01.pdf

  [2]: Guo-Xun Yuan, Chia-Hua Ho and Chih-Jen Lin. An Improved GLMNET for
       L1-regularized Logistic Regression. _Journal of Machine Learning
       Research_, 13, 2012.
       http://www.jmlr.org/papers/volume13/yuan12a/yuan12a.pdf
  """
  graph_deps = [
      model_matrix,
      response,
      model_coefficients_start,
      l1_regularizer,
      l2_regularizer,
      maximum_iterations,
      maximum_full_sweeps_per_iteration,
      # TODO(b/111925792): Replace `tolerance` arg with something like
      # `convergence_criteria_fn`.
      tolerance,
      learning_rate,
      model_coefficients_update_var,
  ]
  with tf.name_scope(name, 'fit_sparse', graph_deps):
    # TODO(b/111922388): Include dispersion and offset parameters.
    def _grad_neg_log_likelihood_and_fim_fn(x):
      predicted_linear_response = _sparse_or_dense_matvecmul(model_matrix, x)
      g, h_middle = _grad_neg_log_likelihood_and_fim(
          model_matrix, predicted_linear_response, response, model)
      return g, model_matrix, h_middle

    return minimize_sparse(
        _grad_neg_log_likelihood_and_fim_fn,
        x_start=model_coefficients_start,
        l1_regularizer=l1_regularizer,
        l2_regularizer=l2_regularizer,
        maximum_iterations=maximum_iterations,
        maximum_full_sweeps_per_iteration=maximum_full_sweeps_per_iteration,
        learning_rate=learning_rate,
        tolerance=tolerance,
        x_update_var=model_coefficients_update_var,
        name=name)


def _fit_sparse_exact_hessian(model_matrix,  # pylint: disable = missing-docstring
                              response,
                              model,
                              model_coefficients_start,
                              tolerance,
                              l1_regularizer,
                              l2_regularizer=None,
                              maximum_iterations=None,
                              maximum_full_sweeps_per_iteration=1,
                              learning_rate=None,
                              model_coefficients_update_var=None,
                              name=None):
  graph_deps = [
      model_matrix,
      response,
      model_coefficients_start,
      l1_regularizer,
      l2_regularizer,
      maximum_iterations,
      maximum_full_sweeps_per_iteration,
      # TODO(b/111925792): Replace `tolerance` arg with something like
      # `convergence_criteria_fn`.
      tolerance,
      learning_rate,
      model_coefficients_update_var,
  ]
  with tf.name_scope(name, 'fit_sparse_exact_hessian', graph_deps):
    # TODO(b/111922388): Include dispersion and offset parameters.
    def _neg_log_likelihood(x):
      predicted_linear_response = _sparse_or_dense_matvecmul(model_matrix, x)
      log_probs = model.log_prob(response, predicted_linear_response)
      return -log_probs

    def _grad_and_hessian_loss_fn(x):
      loss = _neg_log_likelihood(x)
      grad_loss = tf.gradients(loss, [x])[0]
      hessian_loss = tf.hessians(loss, [x])[0]
      hessian_chol = tf.cholesky(hessian_loss)
      return grad_loss, hessian_chol, tf.ones_like(grad_loss)

    return minimize_sparse(
        _grad_and_hessian_loss_fn,
        x_start=model_coefficients_start,
        l1_regularizer=l1_regularizer,
        l2_regularizer=l2_regularizer,
        maximum_iterations=maximum_iterations,
        maximum_full_sweeps_per_iteration=maximum_full_sweeps_per_iteration,
        learning_rate=learning_rate,
        tolerance=tolerance,
        x_update_var=model_coefficients_update_var,
        name=name)
