# Copyright (C) 2019 ATHENA AUTHORS; Xiangang Li; Dongwei Jiang; Wubo Li
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
# ==============================================================================
# Only support eager mode and TF>=2.0.0
# pylint: disable=no-member, invalid-name, relative-beyond-top-level
# pylint: disable=too-many-locals, too-many-statements, too-many-arguments, too-many-instance-attributes
r""" an implementations for APC
"""

import tensorflow as tf
from .base import BaseModel
from ..utils.hparam import register_and_parse_hparams
from ..layers.commons import PositionalEncoding
from ..layers.transformer import TransformerEncoder, TransformerEncoderLayer
from ..loss import APCLoss


class AutoregressivePredictCoding(BaseModel):
    """ implementation for APC pretrain model
    Args:
        num_filters: a int type number, i.e the number of filters in cnn
        d_model: a int type number, i.e dimension of model
        num_heads: number of heads in transformer
        num_encoder_layers: number of layer in encoder
        dff: a int type number, i.e dimension of model
        rate: rate of dropout layers
        chunk_size: number of consecutive masks, i.e 1 or 3
        keep_probability: probability not to be masked
    """
    default_config = {
        "return_encoder_output": False,
        "num_filters": 512,
        "d_model": 512,
        "num_heads": 8,
        "num_encoder_layers": 12,
        "dff": 1280,
        "rate": 0.1,
        "chunk_size": 1,
        "keep_probability": 0.8,
        "input_dropout_rate": 0.0
    }

    def __init__(self, data_descriptions, config=None):
        super().__init__()
        self.downsample_scale = 4
        self.num_class = data_descriptions.num_class * self.downsample_scale

        # default settings
        self.hparams = register_and_parse_hparams(self.default_config, config, cls=self.__class__)

        # APC loss fuction and metric
        _, self.dim, self.num_channels = data_descriptions.sample_shape["input"].as_list()
        self.apc_loss_function = APCLoss()
        self.metric = tf.keras.metrics.Mean(name="AverageLoss")

        num_filters = self.hparams.num_filters
        d_model = self.hparams.d_model
        layers = tf.keras.layers
        input_features = layers.Input(
            shape=data_descriptions.sample_shape["input"],
            dtype=tf.float32
        )
        inner = layers.Conv2D(
            filters=num_filters,
            kernel_size=(3, 3),
            strides=(2, 2),
            padding="same",
            use_bias=False,
            data_format="channels_last",
        )(input_features)
        inner = layers.BatchNormalization()(inner)
        inner = tf.nn.relu6(inner)
        inner = layers.Conv2D(
            filters=num_filters,
            kernel_size=(3, 3),
            strides=(2, 2),
            padding="same",
            use_bias=False,
            data_format="channels_last",
        )(inner)
        inner = layers.BatchNormalization()(inner)

        inner = tf.nn.relu6(inner)
        _, _, dim, channels = inner.get_shape().as_list()
        output_dim = dim * channels
        inner = layers.Reshape((-1, output_dim))(inner)

        inner = layers.Dense(d_model, activation=tf.nn.relu6)(inner)
        inner = PositionalEncoding(d_model, scale=False)(inner)
        inner = layers.Dropout(self.hparams.rate)(inner)  # self.hparams.rate
        self.x_net = tf.keras.Model(inputs=input_features, outputs=inner, name="x_net")
        print(self.x_net.summary())

        encoder_layers = [
            TransformerEncoderLayer(
                self.hparams.d_model,
                self.hparams.num_heads,
                self.hparams.dff,
                self.hparams.rate,
                "gelu",
            )
            for _ in range(self.hparams.num_encoder_layers)
        ]
        self.encoder = TransformerEncoder(encoder_layers)
        self.final_layer = layers.Dense(self.num_class, input_shape=(d_model,))
        self.randomizer = tf.random_uniform_initializer(0, 1)

    def call(self, samples, training: bool = None):
        """ used for training
        Args:
            samples is a dict, including keys: 'input', 'input_length', 'output_length', 'output'
                input: acoustic features, Tensor, shape is (batch, time_len, dim, 1), i.e f-bank
        Return:
            APC outputs to fit acoustic features
                encoder_outputs: Transformer encoder outputs, Tensor, shape is (batch, seqlen, dim)
        """

        x0 = tf.nn.dropout(samples["input"], self.hparams.input_dropout_rate)
        x = self.x_net(x0, training=training)
        x = self.encoder(x, None, training=training, unidirectional=True)
        logits = self.final_layer(x, training=training)

        return logits

    def get_loss(self, logits, samples, training=None):
        """get APC loss
        Args:
            logitsdd: APC output
        Return:
            APC L1 loss
        """

        logit_length = self.compute_logit_length(samples)
        loss = self.apc_loss_function(logits, samples, logit_length)
        self.metric.update_state(loss)
        metrics = {self.metric.name: self.metric.result()}
        return loss, metrics

    def compute_logit_length(self, samples):
        input_length = tf.cast(samples["input_length"], tf.float32)
        logit_length = tf.math.ceil(input_length / 2)
        logit_length = tf.math.ceil(logit_length / 2)
        logit_length = tf.cast(logit_length, tf.int32)
        return logit_length

    def prepare_samples(self, samples):
        """ for special data prepare
        carefully: do not change the shape of samples
        """
        apc_data = samples["input"]

        seq_len = (
            tf.math.floordiv(tf.shape(apc_data)[1], self.downsample_scale)
            * self.downsample_scale
        )
        apc_data = apc_data[:, :seq_len, :, :]
        batch_size, seq_len, dim, num_channels = tf.shape(apc_data)
        zeros = tf.zeros([batch_size, 5, dim, num_channels])
        apc_out = tf.concat([apc_data[:, 5:, :, :], zeros], 1)
        apc_output_length = samples["input_length"] - 5
        samples["input"] = apc_data
        samples["input_length"] = samples["input_length"]
        samples["output"] = tf.squeeze(apc_out,-1)
        samples["output_length"] = apc_output_length

        return samples
