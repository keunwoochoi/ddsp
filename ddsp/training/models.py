# Copyright 2020 The DDSP Authors.
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

# Lint as: python3
"""Model that outputs coefficeints of an additive synthesizer."""

import time

from absl import logging
import ddsp
from ddsp.training import train_util
from ddsp import untrained_models
import gin
import tensorflow.compat.v2 as tf

tfkl = tf.keras.layers


@gin.configurable
def get_model(model=gin.REQUIRED):
  """Gin configurable function get a 'global' model for use in ddsp_run.py.

  Convenience for using the same model in train(), evaluate(), and sample().
  Args:
    model: An instantiated model, such as 'models.Autoencoder()'.

  Returns:
    The 'global' model specifieed in the gin config.
  """
  return model


class Model(tf.keras.Model):
  """Wrap the model function for dependency injection with gin."""

  def __init__(self, losses=None, name='model'):
    super().__init__(name=name)
    self.loss_objs = ddsp.core.make_iterable(losses)
    self.loss_names = [loss_obj.name
                       for loss_obj in self.loss_objs] + ['total_loss']

  @property
  def losses_dict(self):
    """For metrics, returns dict {loss_name: loss_value}."""
    losses_dict = dict(zip(self.loss_names, self.losses))
    losses_dict['total_loss'] = tf.reduce_sum(self.losses)
    return losses_dict

  def add_losses(self, audio, audio_gen):
    """Add losses for generated audio."""
    for loss_obj in self.loss_objs:
      self.add_loss(loss_obj(audio, audio_gen))

  def restore(self, checkpoint_path):
    """Restore model and optimizer from a checkpoint."""
    start_time = time.time()
    latest_checkpoint = train_util.get_latest_chekpoint(checkpoint_path)
    if latest_checkpoint is not None:
      checkpoint = tf.train.Checkpoint(model=self)
      checkpoint.restore(latest_checkpoint).expect_partial()
      logging.info('Loaded checkpoint %s', latest_checkpoint)
      logging.info('Loading model took %.1f seconds', time.time() - start_time)
    else:
      logging.info('Could not find checkpoint to load at %s, skipping.',
                   checkpoint_path)


@gin.configurable
class Autoencoder(Model):
  """Wrap the model function for dependency injection with gin."""

  def __init__(self,
               preprocessor=None,
               encoder=None,
               decoder=None,
               processor_group=None,
               losses=None,
               name='autoencoder'):
    super().__init__(name=name, losses=losses)
    self.preprocessor = preprocessor
    self.encoder = encoder
    self.decoder = decoder
    self.processor_group = processor_group

  def encode(self, features, training=True):
    """Get conditioning by preprocessing then encoding."""
    conditioning = self.preprocessor(features, training=training)
    return conditioning if self.encoder is None else self.encoder(conditioning)

  def decode(self, conditioning, training=True):
    """Get generated audio by decoding than processing."""
    processor_inputs = self.decoder(conditioning, training=training)
    return self.processor_group(processor_inputs)

  def call(self, features, training=True):
    """Run the core of the network, get predictions and loss."""
    conditioning = self.encode(features, training=training)
    audio_gen = self.decode(conditioning, training=training)
    if training:
      self.add_losses(features['audio'], audio_gen)
    return audio_gen

  def get_controls(self, features, keys=None, training=False):
    """Returns specific processor_group controls."""
    conditioning = self.encode(features, training=training)
    processor_inputs = self.decoder(conditioning)
    controls = self.processor_group.get_controls(processor_inputs)
    # If wrapped in tf.function, only calculates keys of interest.
    return controls if keys is None else {k: controls[k] for k in keys}


@gin.configurable
class AutoencoderDdspice(Autoencoder):
  """Wrap the model function for dependency injection with gin."""

  def __init__(self,
               preprocessor=None,
               encoder=None,
               decoder=None,
               processor_group=None,
               losses=None,
               name='autoencoder_ddspice'):

    super().__init__(preprocessor=preprocessor,
                                             encoder=encoder,
                                             decoder=decoder,
                                             processor_group=processor_group,
                                             losses=losses,
                                             name=name)

    self.trainable_crepe = untrained_models.TrainableCREPE(
      model_capacity='tiny',
      activation_layer='classifier')
    self.pitch_loss_obj = tf.keras.losses.Huber()


  def encode(self, features, training=True):
    """Get conditioning by preprocessing then encoding.
    DDSPICE modification - add the (trainable) crepe predicted pitch
    """
    f0_hz, f0_confidence = self._crepe_predict_pitch(features['audio'])
    features['f0_hz'] = f0_hz
    features['f0_confidence'] = f0_confidence

    conditioning = self.preprocessor(features, training=training)
    return conditioning if self.encoder is None else self.encoder(conditioning)

  def decode(self, conditioning, training=True):
    """Get generated audio by decoding than processing."""
    processor_inputs = self.decoder(conditioning, training=training)
    return self.processor_group(processor_inputs)

  def call(self, features, training=True):
    """Run the core of the network, get predictions and loss."""
    conditioning = self.encode(features, training=training)
    audio_gen = self.decode(conditioning, training=training)
    if training:
      self.add_losses(features['audio'], audio_gen)

    f0_hz_shift, f0_confidence_shift = self._crepe_predict_pitch(features['shifted_audio'])
    self._add_pitch_loss(features['pitch_shift_steps'],
                         f0_hz_shift,
                         features['f0_hz'])
    return audio_gen

  def get_controls(self, features, keys=None, training=False):
    """Returns specific processor_group controls."""
    conditioning = self.encode(features, training=training)
    processor_inputs = self.decoder(conditioning)
    controls = self.processor_group.get_controls(processor_inputs)
    # If wrapped in tf.function, only calculates keys of interest.
    return controls if keys is None else {k: controls[k] for k in keys}

  def _add_pitch_loss(self, pitch_shift_steps, f0_hz_shift, f0_hz):
    """add pitch loss"""
    pitch_shift_steps = tf.expand_dims(pitch_shift_steps, axis=1)  # (16, 1)
    pitch_shift_steps = pitch_shift_steps * tf.ones_like(f0_hz_shift - f0_hz)  # (16, 1000)

    self.add_loss(self.pitch_loss_obj(pitch_shift_steps,
                                      f0_hz_shift - f0_hz))

  def _crepe_predict_pitch(self, audio):
    """
    Args:
      audio: tensor shape of (batch, 64000)

    Returns:
      f0_hz, f0_confidence
    """
    def softargmax(x, beta=1e6, name='softargmax'):
      """
      Approximating argmax. beta=1e5 turns out to be large enough.

      Args:
        x: a 3-dim tensor, (batch, time, axis-to-reduce)

      Returns:
        Approximated argmax tensor shape of (batch, time)
      """
      x_range = tf.range(x.shape.as_list()[-1], dtype=x.dtype)  # shape: (N, )
      for _ in range(2):
        x_range = tf.expand_dims(x_range, 0)   # shape: (1, 1, N)
      return tf.reduce_sum(tf.nn.softmax(x * beta) * x_range, axis=-1, name=name)

    salience = self.trainable_crepe(audio)  # (batch, 1000, 360)
    pitch_idxs = softargmax(salience, name='pitch_idxs')
    # todo: crepe.core.to_local_average_cents should be applied here.. right?
    # but for now; just a simple argmax for temporary.
    # see crepe.core.py L95.
    cent_pred = 20.0 * pitch_idxs + tf.constant(1997.3794084, dtype=tf.float32)
    f0_hz = 10.0 * tf.math.pow(2.0, (cent_pred / 1200.0))
    f0_confidence = tf.math.reduce_max(salience, axis=-1)

    # todo; to think - how do we make sure this salience would mean certain..
    # todo; ..frequency[hz]?? why would it learn that??

    # features['audio'] --> shape=(16, 64000)
    # todo; f0 = untrained_crepe(audio)
    # then pass it to self.preprocessor
    # f0 should be (16, 1000, 1)
    return f0_hz, f0_confidence
