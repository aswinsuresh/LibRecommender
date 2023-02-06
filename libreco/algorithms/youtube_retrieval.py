"""

Reference: Paul Covington et al.  "Deep Neural Networks for YouTube Recommendations"
           (https://static.googleusercontent.com/media/research.google.com/zh-CN//pubs/archive/45530.pdf)

author: massquantity

"""
import numpy as np
from tensorflow.keras.initializers import glorot_uniform
from tensorflow.keras.initializers import zeros as tf_zeros

from ..bases import EmbedBase, ModelMeta
from ..tfops import (
    dense_nn,
    dropout_config,
    multi_sparse_combine_embedding,
    reg_config,
    sess_config,
    tf,
)
from ..utils.misc import count_params
from ..utils.validate import (
    check_dense_values,
    check_seq_mode,
    check_multi_sparse,
    check_sparse_indices,
    dense_field_size,
    sparse_feat_size,
    sparse_field_size,
    true_sparse_field_size,
)


class YouTubeRetrieval(EmbedBase, metaclass=ModelMeta, backend="tensorflow"):
    """
    The model implemented mainly corresponds to the candidate generation
    phase based on the original paper.
    """

    item_variables = ["item_interaction_features", "nce_weights", "nce_biases"]
    sparse_variables = ["sparse_features"]
    dense_variables = ["dense_features"]

    def __init__(
        self,
        task="ranking",
        data_info=None,
        loss_type="sampled_softmax",
        embed_size=16,
        n_epochs=20,
        lr=0.001,
        lr_decay=False,
        epsilon=1e-5,
        hidden_units="128,64",
        reg=None,
        batch_size=256,
        use_bn=True,
        dropout_rate=None,
        num_sampled_per_batch=None,
        sampler="uniform",
        recent_num=10,
        random_num=None,
        multi_sparse_combiner="sqrtn",
        seed=42,
        lower_upper_bound=None,
        tf_sess_config=None,
    ):
        super().__init__(task, data_info, embed_size, lower_upper_bound)

        assert task == "ranking", "YouTube-type models is only suitable for ranking"
        if len(data_info.item_col) > 0:
            raise ValueError("The `YouTuBeRetrieval` model assumes no item features.")
        self.all_args = locals()
        self.loss_type = loss_type
        self.n_epochs = n_epochs
        self.lr = lr
        self.lr_decay = lr_decay
        self.epsilon = epsilon
        self.hidden_units = list(map(int, hidden_units.split(","))) + [self.embed_size]
        self.reg = reg_config(reg)
        self.batch_size = batch_size
        self.use_bn = use_bn
        self.dropout_rate = dropout_config(dropout_rate)
        self.num_sampled_per_batch = num_sampled_per_batch
        self.sampler = sampler
        self.seed = seed
        self.sess = sess_config(tf_sess_config)
        self.seq_mode, self.max_seq_len = check_seq_mode(recent_num, random_num)
        self.recent_seq_indices, self.recent_seq_values = self._set_recent_seqs()
        self.sparse = check_sparse_indices(data_info)
        self.dense = check_dense_values(data_info)
        if self.sparse:
            self.sparse_feature_size = sparse_feat_size(data_info)
            self.sparse_field_size = sparse_field_size(data_info)
            self.multi_sparse_combiner = check_multi_sparse(
                data_info, multi_sparse_combiner
            )
            self.true_sparse_field_size = true_sparse_field_size(
                data_info, self.sparse_field_size, self.multi_sparse_combiner
            )
        if self.dense:
            self.dense_field_size = dense_field_size(data_info)

    def build_model(self):
        tf.set_random_seed(self.seed)
        # item_indices actually serve as labels in YouTuBeRetrieval model
        self.item_indices = tf.placeholder(tf.int64, shape=[None])
        self.is_training = tf.placeholder_with_default(False, shape=[])
        self.concat_embed = []

        self._build_item_interaction()
        self._build_variables()
        if self.sparse:
            self._build_sparse()
        if self.dense:
            self._build_dense()

        concat_features = tf.concat(self.concat_embed, axis=1)
        self.user_vector_repr = dense_nn(
            concat_features,
            self.hidden_units,
            use_bn=self.use_bn,
            dropout_rate=self.dropout_rate,
            is_training=self.is_training,
        )
        count_params()

    def _build_item_interaction(self):
        self.item_interaction_indices = tf.placeholder(tf.int64, shape=[None, 2])
        self.item_interaction_values = tf.placeholder(tf.int32, shape=[None])
        self.modified_batch_size = tf.placeholder(tf.int32, shape=[])

        item_interaction_features = tf.get_variable(
            name="item_interaction_features",
            shape=[self.n_items, self.embed_size],
            initializer=glorot_uniform,
            regularizer=self.reg,
        )
        sparse_item_interaction = tf.SparseTensor(
            self.item_interaction_indices,
            self.item_interaction_values,
            [self.modified_batch_size, self.n_items],
        )
        pooled_embed = tf.nn.safe_embedding_lookup_sparse(
            item_interaction_features,
            sparse_item_interaction,
            sparse_weights=None,
            combiner="sqrtn",
            default_id=None,
        )  # unknown user will return 0-vector
        self.concat_embed.append(pooled_embed)

    def _build_sparse(self):
        self.sparse_indices = tf.placeholder(
            tf.int32, shape=[None, self.sparse_field_size]
        )
        sparse_features = tf.get_variable(
            name="sparse_features",
            shape=[self.sparse_feature_size, self.embed_size],
            initializer=glorot_uniform,
            regularizer=self.reg,
        )

        if self.data_info.multi_sparse_combine_info and self.multi_sparse_combiner in (
            "sum",
            "mean",
            "sqrtn",
        ):
            sparse_embed = multi_sparse_combine_embedding(
                self.data_info,
                sparse_features,
                self.sparse_indices,
                self.multi_sparse_combiner,
                self.embed_size,
            )
        else:
            sparse_embed = tf.nn.embedding_lookup(sparse_features, self.sparse_indices)

        sparse_embed = tf.reshape(
            sparse_embed, [-1, self.true_sparse_field_size * self.embed_size]
        )
        self.concat_embed.append(sparse_embed)

    def _build_dense(self):
        self.dense_values = tf.placeholder(
            tf.float32, shape=[None, self.dense_field_size]
        )
        dense_values_reshape = tf.reshape(
            self.dense_values, [-1, self.dense_field_size, 1]
        )
        batch_size = tf.shape(self.dense_values)[0]

        dense_features = tf.get_variable(
            name="dense_features",
            shape=[self.dense_field_size, self.embed_size],
            initializer=glorot_uniform,
            regularizer=self.reg,
        )

        dense_embed = tf.expand_dims(dense_features, axis=0)
        # B * F2 * K
        dense_embed = tf.tile(dense_embed, [batch_size, 1, 1])
        dense_embed = tf.multiply(dense_embed, dense_values_reshape)
        dense_embed = tf.reshape(
            dense_embed, [-1, self.dense_field_size * self.embed_size]
        )
        self.concat_embed.append(dense_embed)

    def _build_variables(self):
        self.nce_weights = tf.get_variable(
            name="nce_weights",
            # n_classes, embed_size
            shape=[self.n_items, self.embed_size],
            initializer=glorot_uniform,
            regularizer=self.reg,
        )
        self.nce_biases = tf.get_variable(
            name="nce_biases",
            shape=[self.n_items],
            initializer=tf_zeros,
            regularizer=self.reg,
            trainable=True,
        )

    def _set_recent_seqs(self):
        interacted_indices = []
        interacted_items = []
        for u in range(self.n_users):
            u_consumed_items = self.user_consumed[u]
            u_items_len = len(u_consumed_items)
            if u_items_len < self.max_seq_len:
                interacted_indices.extend([u] * u_items_len)
                interacted_items.extend(u_consumed_items)
            else:
                interacted_indices.extend([u] * self.max_seq_len)
                interacted_items.extend(u_consumed_items[-self.max_seq_len:])

        interacted_indices = np.array(interacted_indices).reshape(-1, 1)
        indices = np.concatenate(
            [interacted_indices, np.zeros_like(interacted_indices)], axis=1
        )
        return indices, interacted_items

    def set_embeddings(self):
        feed_dict = {
            self.item_interaction_indices: self.recent_seq_indices,
            self.item_interaction_values: self.recent_seq_values,
            self.modified_batch_size: self.n_users,
            self.is_training: False,
        }
        if self.sparse:
            # remove oov
            user_sparse_indices = self.data_info.user_sparse_unique[:-1]
            feed_dict.update({self.sparse_indices: user_sparse_indices})
        if self.dense:
            user_dense_values = self.data_info.user_dense_unique[:-1]
            feed_dict.update({self.dense_values: user_dense_values})

        user_vector = self.sess.run(self.user_vector_repr, feed_dict)
        item_weights = self.sess.run(self.nce_weights)
        item_biases = self.sess.run(self.nce_biases)

        user_bias = np.ones([len(user_vector), 1], dtype=user_vector.dtype)
        item_bias = item_biases[:, None]
        self.user_embed = np.hstack([user_vector, user_bias])
        self.item_embed = np.hstack([item_weights, item_bias])
