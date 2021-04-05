
from libs.notification_item import NotificationItem  # pylint: disable=E0611, E0401
from libs.notification_enum import NotificationEnum  # pylint: disable=E0611, E0401
from libs.config_service import ConfigService  # pylint: disable=E0611, E0401
from libs.audio_device import AudioDevice  # pylint: disable=E0611, E0401
from libs.fps_limiter import FPSLimiter  # pylint: disable=E0611, E0401
from libs.audio_info import AudioInfo  # pylint: disable=E0611, E0401
from libs.dsp import DSP  # pylint: disable=E0611, E0401


from multiprocessing import Queue
from queue import Empty

from time import time
import numpy as np
import pyaudio
import logging


class AudioProcessService:
    def start(self, config_lock, notification_queue_in, notification_queue_out, audio_queue, py_audio):
        self.logger = logging.getLogger(__name__)

        self._config_lock = config_lock
        self._notification_queue_in = notification_queue_in
        self._notification_queue_out = notification_queue_out
        self._audio_queue = audio_queue

        self.audio_buffer_queue = Queue(2)
        self._py_audio = py_audio
        self.stream = None

        self.init_audio_service(show_output=True)

        while True:
            try:
                self.audio_service_routine()
                self._fps_limiter.fps_limiter()
            except KeyboardInterrupt:
                break

    def init_audio_service(self, show_output=False):
        try:
            # Initial config load.
            ConfigService.instance(self._config_lock).load_config()
            self._config = ConfigService.instance(self._config_lock).config

            # Init FPS Limiter.
            self._fps_limiter = FPSLimiter(120)
            self._skip_routine = False
            self._devices = AudioInfo.GetAudioDevices(self._py_audio)

            self.log_output(show_output, logging.INFO, "Found the following audio sources:")

            # Select the audio device you want to use.
            selected_device_list_index = 0
            try:
                selected_device_list_index = int(self._config["general_settings"]["DEVICE_ID"])
            except Exception as e:
                self.logger.exception(f"Could not parse audio id: {e}")

            # Check if the index is inside the list.
            self.selected_device = None
            # For each audio device, add to list of devices.
            for current_audio_device in self._devices:

                if current_audio_device.id == selected_device_list_index:
                    self.selected_device = current_audio_device

            self.logger.debug(f"Selected Device: {self.selected_device}")

            # Could not find a mic with the selected mic id, so I will use the first device I found.
            if self.selected_device is None:
                self.log_output(show_output, logging.ERROR, "********************************************************")
                self.log_output(show_output, logging.ERROR, "*                      Error                           *")
                self.log_output(show_output, logging.ERROR, "********************************************************")
                self.log_output(show_output, logging.ERROR, f"Could not find the mic with the id: {selected_device_list_index}")
                self.log_output(show_output, logging.ERROR, "Using the first mic as fallback.")
                self.log_output(show_output, logging.ERROR, "Please change the id of the mic inside the config.")
                self.selected_device = self._devices[0]

            self._device_rate = self._config["general_settings"]["DEFAULT_SAMPLE_RATE"]
            self._frames_per_buffer = self._config["general_settings"]["FRAMES_PER_BUFFER"]
            self.n_fft_bins = self._config["general_settings"]["N_FFT_BINS"]
            self.log_output(show_output, logging.INFO, f"Selected Device: {self.selected_device.ToString()}")

            # Init Timer
            self.start_time_1 = time()
            self.ten_seconds_counter_1 = time()
            self.start_time_2 = time()
            self.ten_seconds_counter_2 = time()

            self._dsp = DSP(self._config)

            self.audio = np.empty((self._frames_per_buffer), dtype="int16")

            # Reinit buffer queue
            self.audio_buffer_queue = Queue(2)

            # callback function to stream audio, another thread.
            def callback(in_data, frame_count, time_info, status):
                if self._skip_routine:
                    return (self.audio, pyaudio.paContinue)

                try:
                    self.audio_buffer_queue.put(in_data)
                except Exception as e:
                    pass

                self.end_time_1 = time()

                if time() - self.ten_seconds_counter_1 > 10:
                    self.ten_seconds_counter_1 = time()
                    time_dif = self.end_time_1 - self.start_time_1
                    fps = 1 / time_dif
                    self.logger.info(f"Callback | FPS: {fps:.2f}")

                self.start_time_1 = time()

                return (self.audio, pyaudio.paContinue)

            self.log_output(show_output, logging.DEBUG, "Starting Open Audio Stream...")
            self.stream = self._py_audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self._device_rate,
                input=True,
                input_device_index=self.selected_device.id,
                frames_per_buffer=self._frames_per_buffer,
                stream_callback=callback
            )
        except Exception as e:
            self.logger.error("Could not init AudioService.")
            self.logger.exception(f"Unexpected error in init_audio_service: {e}")

    def log_output(self, show_output, log_level, message):
        if show_output:
            if log_level == logging.INFO:
                self.logger.info(message)
            elif log_level == logging.DEBUG:
                self.logger.debug(message)
            elif log_level == logging.ERROR:
                self.logger.error(message)
            else:
                self.logger.debug(message)
        else:
            self.logger.debug(message)

    def audio_service_routine(self):
        try:
            if not self._notification_queue_in.empty():
                current_notification_item = self._notification_queue_in.get()

                if current_notification_item.notification_enum is NotificationEnum.config_refresh:
                    if self.stream is not None:
                        self.stream.stop_stream()
                        self.stream.close()
                    self.init_audio_service()
                    self._notification_queue_out.put(NotificationItem(NotificationEnum.config_refresh_finished, current_notification_item.device_id))
                elif current_notification_item.notification_enum is NotificationEnum.process_continue:
                    self._skip_routine = False
                elif current_notification_item.notification_enum is NotificationEnum.process_pause:
                    self._skip_routine = True

            if self._skip_routine:
                return

            try:
                in_data = self.audio_buffer_queue.get(block=True, timeout=1)
            except Empty as e:
                self.logger.debug("Audio in timeout. Queue is Empty")
                return

            # Convert the raw string audio stream to an array.
            y = np.fromstring(in_data, dtype=np.int16)
            # Use the type float32.
            y = y.astype(np.float32)

            # Process the audio stream.
            audio_datas = self._dsp.update(y)

            # Check if value is higher than min value.
            if audio_datas["vol"] < self._config["general_settings"]["MIN_VOLUME_THRESHOLD"]:
                # Fill the array with zeros, to fade out the effect.
                audio_datas["mel"] = np.zeros(self.n_fft_bins)

            if self._audio_queue.full():
                try:
                    pre_audio_data = self._audio_queue.get(block=True, timeout=0.033)
                    del pre_audio_data
                except Exception as e:
                    pass

            self._audio_queue.put(audio_datas, False)

            self.end_time_2 = time()

            if time() - self.ten_seconds_counter_2 > 10:
                self.ten_seconds_counter_2 = time()
                time_dif = self.end_time_2 - self.start_time_2
                fps = 1 / time_dif
                self.logger.info(f"Routine | FPS: {fps:.2f}")

            self.start_time_2 = time()

        except IOError:
            self.logger.exception("IOError while reading the Microphone Stream.")
            pass
        except Exception as e:
            self.logger.error("Could not run AudioService routine.")
            self.logger.exception(f"Unexpected error in routine: {e}")
