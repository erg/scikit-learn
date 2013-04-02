"""
This module gathers tree-based methods, including decision, regression and
randomized trees. Single and multi-output problems are both handled.
"""

# Code is originally adapted from MILK: Machine Learning Toolkit
# Copyright (C) 2008-2011, Luis Pedro Coelho <luis@luispedro.org>
# License: MIT. See COPYING.MIT file in the milk distribution

# Authors: Brian Holt, Peter Prettenhofer, Satrajit Ghosh, Gilles Louppe,
#          Noel Dawe
# License: BSD3

from __future__ import division

import numbers
import numpy as np
from abc import ABCMeta, abstractmethod
from warnings import warn

from ..base import BaseEstimator, ClassifierMixin, RegressorMixin
from ..externals import six
from ..externals.six.moves import xrange
from ..feature_selection.selector_mixin import SelectorMixin
from ..utils import array2d, check_random_state
from ..utils.validation import check_arrays

from . import _tree


__all__ = ["DecisionTreeClassifier",
           "DecisionTreeRegressor",
           "ExtraTreeClassifier",
           "ExtraTreeRegressor"]

DTYPE = _tree.DTYPE
DOUBLE = _tree.DOUBLE

CLASSIFICATION = {
    "gini": _tree.Gini,
    "entropy": _tree.Entropy,
}

REGRESSION = {
    "mse": _tree.MSE,
}


def export_graphviz(decision_tree, out_file=None, feature_names=None):
    """Export a decision tree in DOT format.

    This function generates a GraphViz representation of the decision tree,
    which is then written into `out_file`. Once exported, graphical renderings
    can be generated using, for example::

        $ dot -Tps tree.dot -o tree.ps      (PostScript format)
        $ dot -Tpng tree.dot -o tree.png    (PNG format)

    Parameters
    ----------
    decision_tree : decision tree classifier
        The decision tree to be exported to graphviz.

    out : file object or string, optional (default=None)
        Handle or name of the output file.

    feature_names : list of strings, optional (default=None)
        Names of each of the features.

    Returns
    -------
    out_file : file object
        The file object to which the tree was exported.  The user is
        expected to `close()` this object when done with it.

    Examples
    --------
    >>> import os
    >>> from sklearn.datasets import load_iris
    >>> from sklearn import tree

    >>> clf = tree.DecisionTreeClassifier()
    >>> iris = load_iris()

    >>> clf = clf.fit(iris.data, iris.target)
    >>> import tempfile
    >>> export_file = tree.export_graphviz(clf,
    ...     out_file='test_export_graphvix.dot')
    >>> export_file.close()
    >>> os.unlink(export_file.name)
    """
    def node_to_str(tree, node_id):
        value = tree.value[node_id]
        if tree.n_outputs == 1:
            value = value[0, :]

        if tree.children_left[node_id] == _tree.TREE_LEAF:
            return "error = %.4f\\nsamples = %s\\nvalue = %s" \
                   % (tree.init_error[node_id],
                      tree.n_samples[node_id],
                      value)
        else:
            if feature_names is not None:
                feature = feature_names[tree.feature[node_id]]
            else:
                feature = "X[%s]" % tree.feature[node_id]

            return "%s <= %.4f\\nerror = %s\\nsamples = %s\\nvalue = %s" \
                   % (feature,
                      tree.threshold[node_id],
                      tree.init_error[node_id],
                      tree.n_samples[node_id],
                      value)

    def recurse(tree, node_id, parent=None):
        if node_id == _tree.TREE_LEAF:
            raise ValueError("Invalid node_id %s" % _tree.TREE_LEAF)

        left_child = tree.children_left[node_id]
        right_child = tree.children_right[node_id]

        # Add node with description
        out_file.write('%d [label="%s", shape="box"] ;\n' %
                       (node_id, node_to_str(tree, node_id)))

        if parent is not None:
            # Add edge to parent
            out_file.write('%d -> %d ;\n' % (parent, node_id))

        if left_child != _tree.TREE_LEAF:  # and right_child != _tree.TREE_LEAF
            recurse(tree, left_child, node_id)
            recurse(tree, right_child, node_id)

    if out_file is None:
        out_file = "tree.dot"

    if isinstance(out_file, six.string_types):
        if six.PY3:
            out_file = open(out_file, "w", encoding="utf-8")
        else:
            out_file = open(out_file, "wb")

    out_file.write("digraph Tree {\n")
    if isinstance(decision_tree, _tree.Tree):
        recurse(decision_tree, 0)
    else:
        recurse(decision_tree.tree_, 0)
    out_file.write("}")

    return out_file


class Tree(object):
    """Struct-of-arrays representation of a binary decision tree.

    The binary tree is represented as a number of parallel arrays.
    The i-th element of each array holds information about the
    node `i`. You can find a detailed description of all arrays
    below. NOTE: Some of the arrays only apply to either leaves or
    split nodes, resp. In this case the values of nodes of the other
    type are arbitrary!

    Attributes
    ----------
    node_count : int
        Number of nodes (internal nodes + leaves) in the tree.

    children : np.ndarray, shape=(node_count, 2), dtype=int32
        `children[i, 0]` holds the node id of the left child of node `i`.
        `children[i, 1]` holds the node id of the right child of node `i`.
        For leaves `children[i, 0] == children[i, 1] == Tree.LEAF == -1`.

    feature : np.ndarray of int32
        The feature to split on (only for internal nodes).

    threshold : np.ndarray of float64
        The threshold of each node (only for leaves).

    value : np.ndarray of float64, shape=(capacity, n_classes)
        Contains the constant prediction value of each node.

    best_error : np.ndarray of float64
        The error of the (best) split.
        For leaves `init_error == `best_error`.

    init_error : np.ndarray of float64
        The initial error of the node (before splitting).
        For leaves `init_error == `best_error`.

    n_samples : np.ndarray of np.int32
        The number of samples at each node.
    """

    LEAF = -1
    UNDEFINED = -2

    def __init__(self, n_classes, n_features, n_outputs=1, capacity=3):
        self.n_classes = n_classes
        self.n_features = n_features

        self.node_count = 0

        self.children = np.empty((capacity, 2), dtype=np.int32)
        self.children.fill(Tree.UNDEFINED)

        self.feature = np.empty((capacity,), dtype=np.int32)
        self.feature.fill(Tree.UNDEFINED)

        self.threshold = np.empty((capacity,), dtype=np.float64)
        self.value = np.empty((capacity, n_classes), dtype=np.float64)

        self.best_error = np.empty((capacity,), dtype=np.float32)
        self.init_error = np.empty((capacity,), dtype=np.float32)
        self.n_samples = np.empty((capacity,), dtype=np.int32)

    def _resize(self, capacity=None):
        """Resize tree arrays to `capacity`, if `None` double capacity. """
        if capacity is None:
            capacity = int(self.children.shape[0] * 2.0)

        if capacity == self.children.shape[0]:
            return

        self.children.resize((capacity, 2), refcheck=False)
        self.feature.resize((capacity,), refcheck=False)
        self.threshold.resize((capacity,), refcheck=False)
        self.value.resize((capacity, self.value.shape[1]), refcheck=False)
        self.best_error.resize((capacity,), refcheck=False)
        self.init_error.resize((capacity,), refcheck=False)
        self.n_samples.resize((capacity,), refcheck=False)

        # if capacity smaller than node_count, adjust the counter
        if capacity < self.node_count:
            self.node_count = capacity

    def _add_split_node(self, parent, is_left_child, feature, threshold,
                        best_error, init_error, n_samples, value):
        """Add a splitting node to the tree. The new node registers itself as
        the child of its parent. """
        node_id = self.node_count
        if node_id >= self.children.shape[0]:
            self._resize()

        self.feature[node_id] = feature
        self.threshold[node_id] = threshold

        self.init_error[node_id] = init_error
        self.best_error[node_id] = best_error
        self.n_samples[node_id] = n_samples
        self.value[node_id] = value

        # set as left or right child of parent
        if parent > Tree.LEAF:
            if is_left_child:
                self.children[parent, 0] = node_id
            else:
                self.children[parent, 1] = node_id

        self.node_count += 1
        return node_id

    def _add_leaf(self, parent, is_left_child, value, error, n_samples):
        """Add a leaf to the tree. The new node registers itself as the
        child of its parent. """
        node_id = self.node_count
        if node_id >= self.children.shape[0]:
            self._resize()

        self.value[node_id] = value
        self.n_samples[node_id] = n_samples
        self.init_error[node_id] = error
        self.best_error[node_id] = error

        if is_left_child:
            self.children[parent, 0] = node_id
        else:
            self.children[parent, 1] = node_id

        self.children[node_id, :] = Tree.LEAF

        self.node_count += 1
        return node_id

    def _copy(self):
        new_tree = Tree(self.n_classes, self.n_features, self.n_outputs)
        new_tree.node_count = self.node_count
        new_tree.children = self.children.copy()
        new_tree.feature = self.feature.copy()
        new_tree.threshold = self.threshold.copy()
        new_tree.value = self.value.copy()
        new_tree.best_error = self.best_error.copy()
        new_tree.init_error = self.init_error.copy()
        new_tree.n_samples = self.n_samples.copy()

        return new_tree

    @staticmethod
    def _get_leaves(children):
        """Lists the leaves from the children array of a tree object"""
        return np.where(np.all(children == Tree.LEAF, axis=1))[0]

    @property
    def leaves(self):
        return self._get_leaves(self.children)

    def pruning_order(self, max_to_prune=None):
        """Compute the order for which the tree should be pruned.

        The algorithm used is weakest link pruning. It removes first the nodes
        that improve the tree the least.


        Parameters
        ----------
        max_to_prune : int, optional (default=all the nodes)
            maximum number of nodes to prune

        Returns
        -------
        nodes : numpy array
            list of the nodes to remove to get to the optimal subtree.

        References
        ----------

        .. [1] J. Friedman and T. Hastie, "The elements of statistical
        learning", 2001, section 9.2.1

        """

        def _get_terminal_nodes(children):
            """Lists the nodes that only have leaves as children"""
            leaves = self._get_leaves(children)
            child_is_leaf = np.in1d(children, leaves).reshape(children.shape)
            return np.where(np.all(child_is_leaf, axis=1))[0]

        def _next_to_prune(tree, children=None):
            """Weakest link pruning for the subtree defined by children"""

            if children is None:
                children = tree.children

            t_nodes = _get_terminal_nodes(children)
            g_i = tree.init_error[t_nodes] - tree.best_error[t_nodes]

            return t_nodes[np.argmin(g_i)]

        if max_to_prune is None:
            max_to_prune = self.node_count

        children = self.children.copy()
        nodes = list()

        while True:
            node = _next_to_prune(self, children)
            nodes.append(node)

            if (len(nodes) == max_to_prune) or (node == 0):
                return np.array(nodes)

            #Remove the subtree from the children array
            children[children[node], :] = Tree.UNDEFINED
            children[node, :] = Tree.LEAF

    def prune(self, n_leaves):
        """Prunes the tree to obtain the optimal subtree with n_leaves leaves.


        Parameters
        ----------
        n_leaves : int
            The final number of leaves the algorithm should bring

        Returns
        -------
        tree : a Tree object
            returns a new, pruned, tree

        References
        ----------

        .. [1] J. Friedman and T. Hastie, "The elements of statistical
        learning", 2001, section 9.2.1

        """

        to_remove_count = self.node_count - len(self.leaves) - n_leaves + 1
        nodes_to_remove = self.pruning_order(to_remove_count)

        out_tree = self._copy()

        for node in nodes_to_remove:
            #TODO: Add a Tree method to remove a branch of a tree
            out_tree.children[out_tree.children[node], :] = Tree.UNDEFINED
            out_tree.children[node, :] = Tree.LEAF
            out_tree.node_count -= 2

        return out_tree

    def build(self, X, y, criterion, max_depth, min_samples_split,
              min_samples_leaf, min_density, max_features, random_state,
              find_split, sample_mask=None, X_argsorted=None):
        # Recursive algorithm
        def recursive_partition(X, X_argsorted, y, sample_mask, depth,
                                parent, is_left_child):
            # Count samples
            n_node_samples = sample_mask.sum()

            if n_node_samples == 0:
                raise ValueError("Attempting to find a split "
                                 "with an empty sample_mask")

            # Split samples
            if depth < max_depth and n_node_samples >= min_samples_split \
               and n_node_samples >= 2 * min_samples_leaf:
                feature, threshold, best_error, init_error = find_split(
                    X, y, X_argsorted, sample_mask, n_node_samples,
                    min_samples_leaf, max_features, criterion, random_state)
            else:
                feature = -1
                init_error = _tree._error_at_leaf(y, sample_mask, criterion,
                                                  n_node_samples)

            value = criterion.init_value()

            # Current node is leaf
            if feature == -1:
                self._add_leaf(parent, is_left_child, value,
                               init_error, n_node_samples)

            # Current node is internal node (= split node)
            else:
                # Sample mask is too sparse?
                if n_node_samples / X.shape[0] <= min_density:
                    X = X[sample_mask]
                    X_argsorted = np.asfortranarray(
                        np.argsort(X.T, axis=1).astype(np.int32).T)
                    y = y[sample_mask]
                    sample_mask = np.ones((X.shape[0],), dtype=np.bool)

                # Split and and recurse
                split = X[:, feature] <= threshold

                node_id = self._add_split_node(parent, is_left_child, feature,
                                               threshold, best_error,
                                               init_error, n_node_samples,
                                               value)

                # left child recursion
                recursive_partition(X, X_argsorted, y,
                                    np.logical_and(split, sample_mask),
                                    depth + 1, node_id, True)

                # right child recursion
                recursive_partition(X, X_argsorted, y,
                                    np.logical_and(np.logical_not(split),
                                                   sample_mask),
                                    depth + 1, node_id, False)

        # Setup auxiliary data structures and check input before
        # recursive partitioning
        if X.dtype != DTYPE or not np.isfortran(X):
            X = np.asanyarray(X, dtype=DTYPE, order="F")

        if y.dtype != DTYPE or not y.flags.contiguous:
            y = np.ascontiguousarray(y, dtype=DTYPE)

        if sample_mask is None:
            sample_mask = np.ones((X.shape[0],), dtype=np.bool)

        if X_argsorted is None:
            X_argsorted = np.asfortranarray(
                np.argsort(X.T, axis=1).astype(np.int32).T)

        # Pre-allocate some space
        if max_depth <= 10:
            # allocate space for complete binary tree
            init_capacity = (2 ** (max_depth + 1)) - 1
        else:
            # allocate fixed size and dynamically resize later
            init_capacity = 2047

        self._resize(init_capacity)

        # Build the tree by recursive partitioning
        recursive_partition(X, X_argsorted, y, sample_mask, 0, -1, False)

        # Compactify the tree data structure
        self._resize(self.node_count)

        return self

    def predict(self, X):
        out = np.empty((X.shape[0], self.value.shape[1]), dtype=np.float64)

        _tree._predict_tree(X,
                            self.children,
                            self.feature,
                            self.threshold,
                            self.value,
                            out)

        return out

    def compute_feature_importances(self, method="gini"):
        """Computes the importance of each feature (aka variable).

        The following `method`s are supported:

          * "gini" : The difference of the initial error and the error of the
                     split times the number of samples that passed the node.
          * "squared" : The empirical improvement in squared error.

        Parameters
        ----------
        method : str, optional (default="gini")
            The method to estimate the importance of a feature. Either "gini"
            or "squared".
        """
        if method == "gini":
            method = lambda node: (self.n_samples[node] * \
                                     (self.init_error[node] -
                                      self.best_error[node]))
        elif method == "squared":
            method = lambda node: (self.init_error[node] - \
                                   self.best_error[node]) ** 2.0
        else:
            raise ValueError(
                'Invalid value for method. Allowed string '
                'values are "gini", or "mse".')

        importances = np.zeros((self.n_features,), dtype=np.float64)

        for node in range(self.node_count):
            if (self.children[node, 0]
                == self.children[node, 1]
                == Tree.LEAF):
                continue
            else:
                importances[self.feature[node]] += method(node)

        normalizer = np.sum(importances)

        if normalizer > 0.0:
            # Avoid dividing by zero (e.g., when root is pure)
            importances /= normalizer

        return importances


class BaseDecisionTree(BaseEstimator, SelectorMixin):
    """Base class for decision trees.

    Warning: This class should not be used directly.
    Use derived classes instead.
    """
    __metaclass__ = ABCMeta

    @abstractmethod
    def __init__(self,
                 criterion,
                 max_depth,
                 min_samples_split,
                 min_samples_leaf,
                 n_leaves,
                 min_density,
                 max_features,
                 compute_importances,
                 random_state):
        self.criterion = criterion
        self.max_depth = max_depth
        self.min_samples_split = min_samples_split
        self.min_samples_leaf = min_samples_leaf
        self.n_leaves = n_leaves
        self.min_density = min_density
        self.max_features = max_features

        if compute_importances:
            warn("Setting compute_importances=True is no longer "
                 "required. Variable importances are now computed on the fly "
                 "when accessing the feature_importances_ attribute. This "
                 "parameter will be removed in 0.15.", DeprecationWarning)

        self.compute_importances = compute_importances
        self.random_state = random_state

        self.n_features_ = None
        self.n_outputs_ = None
        self.classes_ = None
        self.n_classes_ = None
        self.find_split_ = _tree.TREE_SPLIT_BEST

        self.tree_ = None

    def prune(self, n_leaves):
        """Prunes the decision tree

        This method is necessary to avoid overfitting tree models. While broad
        decision trees should be computed in the first place, pruning them
        allows for smaller trees.

        Parameters
        ----------
        n_leaves : int
            the number of leaves of the pruned tree

        """
        self.tree_ = self.tree_.prune(n_leaves)
        return self

    def fit(self, X, y,
            sample_mask=None, X_argsorted=None,
            check_input=True, sample_weight=None):
        """Build a decision tree from the training set (X, y).

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]
            The training input samples. Use ``dtype=np.float32``
            and ``order='F'`` for maximum efficiency.

        y : array-like, shape = [n_samples] or [n_samples, n_outputs]
            The target values (integers that correspond to classes in
            classification, real numbers in regression).
            Use ``dtype=np.float64`` and ``order='C'`` for maximum
            efficiency.

        sample_mask : array-like, shape = [n_samples], dtype = bool or None
            A bit mask that encodes the rows of ``X`` that should be
            used to build the decision tree. It can be used for bagging
            without the need to create of copy of ``X``.
            If None a mask will be created that includes all samples.

        X_argsorted : array-like, shape = [n_samples, n_features] or None
            Each column of ``X_argsorted`` holds the row indices of ``X``
            sorted according to the value of the corresponding feature
            in ascending order.
            I.e. ``X[X_argsorted[i, k], k] <= X[X_argsorted[j, k], k]``
            for each j > i.
            If None, ``X_argsorted`` is computed internally.
            The argument is supported to enable multiple decision trees
            to share the data structure and to avoid re-computation in
            tree ensembles. For maximum efficiency use dtype np.int32.

        sample_weight : array-like, shape = [n_samples] or None
            Sample weights. If None, then samples are equally weighted. Splits
            that would create child nodes with net zero or negative weight are
            ignored while searching for a split in each node. In the case of
            classification, splits are also ignored if they would result in any
            single class carrying a negative weight in either child node.

        check_input : boolean, (default=True)
            Allow to bypass several input checking.
            Don't use this parameter unless you know what you do.

        Returns
        -------
        self : object
            Returns self.
        """
        if check_input:
            X, y = check_arrays(X, y)
        random_state = check_random_state(self.random_state)

        # Convert data
        if (getattr(X, "dtype", None) != DTYPE or
                X.ndim != 2 or
                not X.flags.fortran):
            X = array2d(X, dtype=DTYPE, order="F")

        n_samples, self.n_features_ = X.shape
        is_classification = isinstance(self, ClassifierMixin)

        y = np.atleast_1d(y)
        if y.ndim == 1:
            # reshape is necessary to preserve the data contiguity against vs
            # [:, np.newaxis] that does not.
            y = np.reshape(y, (-1, 1))

        self.n_outputs_ = y.shape[1]

        if is_classification:
            y = np.copy(y)

            self.classes_ = []
            self.n_classes_ = []

            for k in xrange(self.n_outputs_):
                unique = np.unique(y[:, k])
                self.classes_.append(unique)
                self.n_classes_.append(unique.shape[0])
                y[:, k] = np.searchsorted(unique, y[:, k])

        else:
            self.classes_ = [None] * self.n_outputs_
            self.n_classes_ = [1] * self.n_outputs_

        if getattr(y, "dtype", None) != DOUBLE or not y.flags.contiguous:
            y = np.ascontiguousarray(y, dtype=DOUBLE)

        if is_classification:
            criterion = CLASSIFICATION[self.criterion](self.n_outputs_,
                                                       self.n_classes_)
        else:
            criterion = REGRESSION[self.criterion](self.n_outputs_)

        # Check parameters
        max_depth = np.inf if self.max_depth is None else self.max_depth

        if isinstance(self.max_features, six.string_types):
            if self.max_features == "auto":
                if is_classification:
                    max_features = max(1, int(np.sqrt(self.n_features_)))
                else:
                    max_features = self.n_features_
            elif self.max_features == "sqrt":
                max_features = max(1, int(np.sqrt(self.n_features_)))
            elif self.max_features == "log2":
                max_features = max(1, int(np.log2(self.n_features_)))
            else:
                raise ValueError(
                    'Invalid value for max_features. Allowed string '
                    'values are "auto", "sqrt" or "log2".')
        elif self.max_features is None:
            max_features = self.n_features_
        elif isinstance(self.max_features, (numbers.Integral, np.integer)):
            max_features = self.max_features
        else:  # float
            max_features = int(self.max_features * self.n_features_)

        if len(y) != n_samples:
            raise ValueError("Number of labels=%d does not match "
                             "number of samples=%d" % (len(y), n_samples))
        if self.min_samples_split <= 0:
            raise ValueError("min_samples_split must be greater than zero.")
        if self.min_samples_leaf <= 0:
            raise ValueError("min_samples_leaf must be greater than zero.")
        if max_depth <= 0:
            raise ValueError("max_depth must be greater than zero. ")
        if self.min_density < 0.0 or self.min_density > 1.0:
            raise ValueError("min_density must be in [0, 1]")
        if not (0 < max_features <= self.n_features_):
            raise ValueError("max_features must be in (0, n_features]")

        if sample_mask is not None:
            sample_mask = np.asarray(sample_mask, dtype=np.bool)

            if sample_mask.shape[0] != n_samples:
                raise ValueError("Length of sample_mask=%d does not match "
                                 "number of samples=%d"
                                 % (sample_mask.shape[0], n_samples))

        if sample_weight is not None:
            if (getattr(sample_weight, "dtype", None) != DOUBLE or
                    not sample_weight.flags.contiguous):
                sample_weight = np.ascontiguousarray(
                    sample_weight, dtype=DOUBLE)
            if len(sample_weight.shape) > 1:
                raise ValueError("Sample weights array has more "
                                 "than one dimension: %d" %
                                 len(sample_weight.shape))
            if len(sample_weight) != n_samples:
                raise ValueError("Number of weights=%d does not match "
                                 "number of samples=%d" %
                                 (len(sample_weight), n_samples))

        if X_argsorted is not None:
            X_argsorted = np.asarray(X_argsorted, dtype=np.int32,
                                     order='F')
            if X_argsorted.shape != X.shape:
                raise ValueError("Shape of X_argsorted does not match "
                                 "the shape of X")

        # Set min_samples_split sensibly
        min_samples_split = max(self.min_samples_split,
                                2 * self.min_samples_leaf)

        # Build tree
        self.tree_ = _tree.Tree(self.n_features_, self.n_classes_,
                                self.n_outputs_, criterion, max_depth,
                                min_samples_split, self.min_samples_leaf,
                                self.min_density, max_features,
                                self.find_split_, random_state)

        self.tree_.build(X, y,
                         sample_weight=sample_weight,
                         sample_mask=sample_mask,
                         X_argsorted=X_argsorted)

        if self.n_outputs_ == 1:
            self.n_classes_ = self.n_classes_[0]
            self.classes_ = self.classes_[0]

        if self.n_leaves is not None:
            self.prune(self.n_leaves)

        return self

    def predict(self, X):
        """Predict class or regression value for X.

        For a classification model, the predicted class for each sample in X is
        returned. For a regression model, the predicted value based on X is
        returned.

        Parameters
        ----------
        X : array-like of shape = [n_samples, n_features]
            The input samples.

        Returns
        -------
        y : array of shape = [n_samples] or [n_samples, n_outputs]
            The predicted classes, or the predict values.
        """
        if getattr(X, "dtype", None) != DTYPE or X.ndim != 2:
            X = array2d(X, dtype=DTYPE, order="F")

        n_samples, n_features = X.shape

        if self.tree_ is None:
            raise Exception("Tree not initialized. Perform a fit first")

        if self.n_features_ != n_features:
            raise ValueError("Number of features of the model must "
                             " match the input. Model n_features is %s and "
                             " input n_features is %s "
                             % (self.n_features_, n_features))

        proba = self.tree_.predict(X)

        # Classification
        if isinstance(self, ClassifierMixin):
            if self.n_outputs_ == 1:
                return self.classes_.take(np.argmax(proba[:, 0], axis=1),
                                          axis=0)

            else:
                predictions = np.zeros((n_samples, self.n_outputs_))

                for k in xrange(self.n_outputs_):
                    predictions[:, k] = self.classes_[k].take(
                        np.argmax(proba[:, k], axis=1),
                        axis=0)

                return predictions

        # Regression
        else:
            if self.n_outputs_ == 1:
                return proba[:, 0, 0]

            else:
                return proba[:, :, 0]

    @property
    def feature_importances_(self):
        """Return the feature importances.

        The importance of a feature is computed as the (normalized) total
        reduction of the criterion brought by that feature.
        It is also known as the Gini importance [4]_.

        Returns
        -------
        feature_importances_ : array, shape = [n_features]
        """
        if self.tree_ is None:
            raise ValueError("Estimator not fitted, "
                             "call `fit` before `feature_importances_`.")

        return self.tree_.compute_feature_importances()


class DecisionTreeClassifier(BaseDecisionTree, ClassifierMixin):
    """A decision tree classifier.

    Parameters
    ----------
    criterion : string, optional (default="gini")
        The function to measure the quality of a split. Supported criteria are
        "gini" for the Gini impurity and "entropy" for the information gain.

    max_features : int, float, string or None, optional (default=None)
        The number of features to consider when looking for the best split:
          - If int, then consider `max_features` features at each split.
          - If float, then `max_features` is a percentage and
            `int(max_features * n_features)` features are considered at each
            split.
          - If "auto", then `max_features=sqrt(n_features)`.
          - If "sqrt", then `max_features=sqrt(n_features)`.
          - If "log2", then `max_features=log2(n_features)`.
          - If None, then `max_features=n_features`.

    max_depth : integer or None, optional (default=None)
        The maximum depth of the tree. If None, then nodes are expanded until
        all leaves are pure or until all leaves contain less than
        min_samples_split samples.

    min_samples_split : integer, optional (default=2)
        The minimum number of samples required to split an internal node.

    min_samples_leaf : integer, optional (default=1)
        The minimum number of samples required to be at a leaf node.

    n_leaves : integer, optional (default=None)
        The number of leaves of the post-pruned tree. If None, no post-pruning
        will be run.

    min_density : float, optional (default=0.1)
        This parameter controls a trade-off in an optimization heuristic. It
        controls the minimum density of the `sample_mask` (i.e. the
        fraction of samples in the mask). If the density falls below this
        threshold the mask is recomputed and the input data is packed
        which results in data copying.  If `min_density` equals to one,
        the partitions are always represented as copies of the original
        data. Otherwise, partitions are represented as bit masks (aka
        sample masks).

    random_state : int, RandomState instance or None, optional (default=None)
        If int, random_state is the seed used by the random number generator;
        If RandomState instance, random_state is the random number generator;
        If None, the random number generator is the RandomState instance used
        by `np.random`.

    Attributes
    ----------
    `tree_` : Tree object
        The underlying Tree object.

    `classes_` : array of shape = [n_classes] or a list of such arrays
        The classes labels (single output problem),
        or a list of arrays of class labels (multi-output problem).

    `n_classes_` : int or list
        The number of classes (for single output problems),
        or a list containing the number of classes for each
        output (for multi-output problems).

    `feature_importances_` : array of shape = [n_features]
        The feature importances. The higher, the more important the
        feature. The importance of a feature is computed as the (normalized)
        total reduction of the criterion brought by that feature.  It is also
        known as the Gini importance [4]_.

    See also
    --------
    DecisionTreeRegressor

    References
    ----------

    .. [1] http://en.wikipedia.org/wiki/Decision_tree_learning

    .. [2] L. Breiman, J. Friedman, R. Olshen, and C. Stone, "Classification
           and Regression Trees", Wadsworth, Belmont, CA, 1984.

    .. [3] T. Hastie, R. Tibshirani and J. Friedman. "Elements of Statistical
           Learning", Springer, 2009.

    .. [4] L. Breiman, and A. Cutler, "Random Forests",
           http://www.stat.berkeley.edu/~breiman/RandomForests/cc_home.htm

    Examples
    --------
    >>> from sklearn.datasets import load_iris
    >>> from sklearn.cross_validation import cross_val_score
    >>> from sklearn.tree import DecisionTreeClassifier

    >>> clf = DecisionTreeClassifier(random_state=0)
    >>> iris = load_iris()

    >>> cross_val_score(clf, iris.data, iris.target, cv=10)
    ...                             # doctest: +SKIP
    ...
    array([ 1.     ,  0.93...,  0.86...,  0.93...,  0.93...,
            0.93...,  0.93...,  1.     ,  0.93...,  1.      ])
    """
    def __init__(self,
                 criterion="gini",
                 max_depth=None,
                 min_samples_split=2,
                 min_samples_leaf=1,
                 n_leaves=None,
                 min_density=0.1,
                 max_features=None,
                 compute_importances=False,
                 random_state=None):
        super(DecisionTreeClassifier, self).__init__(criterion,
                                                     max_depth,
                                                     min_samples_split,
                                                     min_samples_leaf,
                                                     n_leaves,
                                                     min_density,
                                                     max_features,
                                                     compute_importances,
                                                     random_state)

    def predict_proba(self, X):
        """Predict class probabilities of the input samples X.

        Parameters
        ----------
        X : array-like of shape = [n_samples, n_features]
            The input samples.

        Returns
        -------
        p : array of shape = [n_samples, n_classes], or a list of n_outputs
            such arrays if n_outputs > 1.
            The class probabilities of the input samples. Classes are ordered
            by arithmetical order.
        """
        if getattr(X, "dtype", None) != DTYPE or X.ndim != 2:
            X = array2d(X, dtype=DTYPE, order="F")

        n_samples, n_features = X.shape

        if self.tree_ is None:
            raise Exception("Tree not initialized. Perform a fit first.")

        if self.n_features_ != n_features:
            raise ValueError("Number of features of the model must "
                             " match the input. Model n_features is %s and "
                             " input n_features is %s "
                             % (self.n_features_, n_features))

        proba = self.tree_.predict(X)

        if self.n_outputs_ == 1:
            proba = proba[:, 0, :self.n_classes_]
            normalizer = proba.sum(axis=1)[:, np.newaxis]
            normalizer[normalizer == 0.0] = 1.0
            proba /= normalizer

            return proba

        else:
            all_proba = []

            for k in xrange(self.n_outputs_):
                proba_k = proba[:, k, :self.n_classes_[k]]
                normalizer = proba_k.sum(axis=1)[:, np.newaxis]
                normalizer[normalizer == 0.0] = 1.0
                proba_k /= normalizer
                all_proba.append(proba_k)

            return all_proba

    def predict_log_proba(self, X):
        """Predict class log-probabilities of the input samples X.

        Parameters
        ----------
        X : array-like of shape = [n_samples, n_features]
            The input samples.

        Returns
        -------
        p : array of shape = [n_samples, n_classes], or a list of n_outputs
            such arrays if n_outputs > 1.
            The class log-probabilities of the input samples. Classes are
            ordered by arithmetical order.
        """
        proba = self.predict_proba(X)

        if self.n_outputs_ == 1:
            return np.log(proba)

        else:
            for k in xrange(self.n_outputs_):
                proba[k] = np.log(proba[k])

            return proba


class DecisionTreeRegressor(BaseDecisionTree, RegressorMixin):
    """A tree regressor.

    Parameters
    ----------
    criterion : string, optional (default="mse")
        The function to measure the quality of a split. The only supported
        criterion is "mse" for the mean squared error.

    max_features : int, float, string or None, optional (default=None)
        The number of features to consider when looking for the best split:
          - If int, then consider `max_features` features at each split.
          - If float, then `max_features` is a percentage and
            `int(max_features * n_features)` features are considered at each
            split.
          - If "auto", then `max_features=n_features`.
          - If "sqrt", then `max_features=sqrt(n_features)`.
          - If "log2", then `max_features=log2(n_features)`.
          - If None, then `max_features=n_features`.

    max_depth : integer or None, optional (default=None)
        The maximum depth of the tree. If None, then nodes are expanded until
        all leaves are pure or until all leaves contain less than
        min_samples_split samples.

    min_samples_split : integer, optional (default=2)
        The minimum number of samples required to split an internal node.

    min_samples_leaf : integer, optional (default=1)
        The minimum number of samples required to be at a leaf node.

    n_leaves : integer, optional (default=None)
        The number of leaves of the post-pruned tree. If None, no post-pruning
        will be run.

    min_density : float, optional (default=0.1)
        This parameter controls a trade-off in an optimization heuristic. It
        controls the minimum density of the `sample_mask` (i.e. the
        fraction of samples in the mask). If the density falls below this
        threshold the mask is recomputed and the input data is packed
        which results in data copying.  If `min_density` equals to one,
        the partitions are always represented as copies of the original
        data. Otherwise, partitions are represented as bit masks (aka
        sample masks).

    random_state : int, RandomState instance or None, optional (default=None)
        If int, random_state is the seed used by the random number generator;
        If RandomState instance, random_state is the random number generator;
        If None, the random number generator is the RandomState instance used
        by `np.random`.

    Attributes
    ----------
    `tree_` : Tree object
        The underlying Tree object.

    `feature_importances_` : array of shape = [n_features]
        The feature importances.
        The higher, the more important the feature.
        The importance of a feature is computed as the
        (normalized)total reduction of the criterion brought
        by that feature. It is also known as the Gini importance [4]_.

    See also
    --------
    DecisionTreeClassifier

    References
    ----------

    .. [1] http://en.wikipedia.org/wiki/Decision_tree_learning

    .. [2] L. Breiman, J. Friedman, R. Olshen, and C. Stone, "Classification
           and Regression Trees", Wadsworth, Belmont, CA, 1984.

    .. [3] T. Hastie, R. Tibshirani and J. Friedman. "Elements of Statistical
           Learning", Springer, 2009.

    .. [4] L. Breiman, and A. Cutler, "Random Forests",
           http://www.stat.berkeley.edu/~breiman/RandomForests/cc_home.htm

    Examples
    --------
    >>> from sklearn.datasets import load_boston
    >>> from sklearn.cross_validation import cross_val_score
    >>> from sklearn.tree import DecisionTreeRegressor

    >>> boston = load_boston()
    >>> regressor = DecisionTreeRegressor(random_state=0)

    R2 scores (a.k.a. coefficient of determination) over 10-folds CV:

    >>> cross_val_score(regressor, boston.data, boston.target, cv=10)
    ...                    # doctest: +SKIP
    ...
    array([ 0.61..., 0.57..., -0.34..., 0.41..., 0.75...,
            0.07..., 0.29..., 0.33..., -1.42..., -1.77...])
    """
    def __init__(self,
                 criterion="mse",
                 max_depth=None,
                 min_samples_split=2,
                 min_samples_leaf=1,
                 n_leaves=None,
                 min_density=0.1,
                 max_features=None,
                 compute_importances=False,
                 random_state=None):
        super(DecisionTreeRegressor, self).__init__(criterion,
                                                    max_depth,
                                                    min_samples_split,
                                                    min_samples_leaf,
                                                    n_leaves,
                                                    min_density,
                                                    max_features,
                                                    compute_importances,
                                                    random_state)


class ExtraTreeClassifier(DecisionTreeClassifier):
    """An extremely randomized tree classifier.

    Extra-trees differ from classic decision trees in the way they are built.
    When looking for the best split to separate the samples of a node into two
    groups, random splits are drawn for each of the `max_features` randomly
    selected features and the best split among those is chosen. When
    `max_features` is set 1, this amounts to building a totally random
    decision tree.

    Warning: Extra-trees should only be used within ensemble methods.

    See also
    --------
    ExtraTreeRegressor, ExtraTreesClassifier, ExtraTreesRegressor

    References
    ----------

    .. [1] P. Geurts, D. Ernst., and L. Wehenkel, "Extremely randomized trees",
           Machine Learning, 63(1), 3-42, 2006.
    """
    def __init__(self,
                 criterion="gini",
                 max_depth=None,
                 min_samples_split=2,
                 min_samples_leaf=1,
                 n_leaves=None,
                 min_density=0.1,
                 max_features="auto",
                 compute_importances=False,
                 random_state=None):
        super(ExtraTreeClassifier, self).__init__(criterion,
                                                  max_depth,
                                                  min_samples_split,
                                                  min_samples_leaf,
                                                  n_leaves,
                                                  min_density,
                                                  max_features,
                                                  compute_importances,
                                                  random_state)

        self.find_split_ = _tree.TREE_SPLIT_RANDOM


class ExtraTreeRegressor(DecisionTreeRegressor):
    """An extremely randomized tree regressor.

    Extra-trees differ from classic decision trees in the way they are built.
    When looking for the best split to separate the samples of a node into two
    groups, random splits are drawn for each of the `max_features` randomly
    selected features and the best split among those is chosen. When
    `max_features` is set 1, this amounts to building a totally random
    decision tree.

    Warning: Extra-trees should only be used within ensemble methods.

    See also
    --------
    ExtraTreeClassifier : A classifier base on extremely randomized trees
    sklearn.ensemble.ExtraTreesClassifier : An ensemble of extra-trees for
        classification
    sklearn.ensemble.ExtraTreesRegressor : An ensemble of extra-trees for
        regression

    References
    ----------

    .. [1] P. Geurts, D. Ernst., and L. Wehenkel, "Extremely randomized trees",
           Machine Learning, 63(1), 3-42, 2006.
    """
    def __init__(self,
                 criterion="mse",
                 max_depth=None,
                 min_samples_split=2,
                 min_samples_leaf=1,
                 n_leaves=None,
                 min_density=0.1,
                 max_features="auto",
                 compute_importances=False,
                 random_state=None):
        super(ExtraTreeRegressor, self).__init__(criterion,
                                                 max_depth,
                                                 min_samples_split,
                                                 min_samples_leaf,
                                                 n_leaves,
                                                 min_density,
                                                 max_features,
                                                 compute_importances,
                                                 random_state)

        self.find_split_ = _tree.TREE_SPLIT_RANDOM


def prune_path(clf, X, y, max_n_leaves=10, n_iterations=10,
                          test_size=0.1, random_state=None):
    """Cross validation of scores for different values of the decision tree.

    This function allows to test what the optimal size of the post-pruned
    decision tree should be. It computes cross validated scores for different
    size of the tree.

    Parameters
    ----------
    clf: decision tree estimator object
        The object to use to fit the data

    X: array-like of shape at least 2D
        The data to fit.

    y: array-like
        The target variable to try to predict.

    max_n_leaves : int, optional (default=10)
        maximum number of leaves of the tree to prune

    n_iterations : int, optional (default=10)
        Number of re-shuffling & splitting iterations.

    test_size : float (default=0.1) or int
        If float, should be between 0.0 and 1.0 and represent the
        proportion of the dataset to include in the test split. If
        int, represents the absolute number of test samples.

    random_state : int or RandomState
        Pseudo-random number generator state used for random sampling.

    Returns
    -------
    scores : list of list of floats
        The scores of the computed cross validated trees grouped by tree size.
        scores[0] correspond to the values of trees of size max_n_leaves and
        scores[-1] to the tree with just two leaves.

    """

    from ..base import clone
    from ..cross_validation import ShuffleSplit

    scores = list()

    kf = ShuffleSplit(len(y), n_iterations, test_size,
                      random_state=random_state)
    for train, test in kf:
        estimator = clone(clf)
        fitted = estimator.fit(X[train], y[train])

        loc_scores = list()
        for i in range(max_n_leaves, 1, -1):
            #We loop from the bigger values to the smaller ones in order to be
            #able to compute the original tree once, and then make it smaller
            fitted.prune(n_leaves=i)
            loc_scores.append(fitted.score(X[test], y[test]))

        scores.append(loc_scores)

    return zip(*scores)
