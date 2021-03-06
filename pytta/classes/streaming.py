# -*- coding: utf-8 -*-

import numpy as np
import sounddevice as sd
from multiprocessing import Event, Queue, Process
from threading import Thread
from queue import Empty
from typing import Optional, Callable, Type, List
from pytta import default
from pytta.classes._base import PyTTaObj
from pytta.classes.signal import SignalObj
from pytta.classes.measurement import Measurement, RecMeasure


class Monitor(object):
    """
    PyTTa default Monitor base class, to have default methods and properties that might be used
    """
    def __init__(self, numsamples: int,
                 samplingrate: int=default.samplingRate,
                 numchannels: List[int]=[len(default.inChannel), len(default.outChannel)],
                 datatype: str='float32'):
        self.samplingRate = samplingrate
        self.numChannels = numchannels
        self.numSamples = numsamples
        self.inData = np.empty((self.numSamples, self.numChannels), dtype=datatype)
        self.outData = np.empty((self.numSamples, self.numChannels), dtype=datatype)
        self.counter = int()
        return

    def setup(self):
        """
        Start up widgets, threads, anything that will be used during audio processing
        """
        pass

    def callback(self, indata: np.ndarray, outdata: np.ndarray, frames: int, status: sd.CallbackFlags):
        """
        The audio processing itself, will be called for every chunk of data taken from the queue
        """
        pass

    def tear_down(self):
        """
        Finish any started object here, to allow the Monitor parallel process be joined
        """
        pass


# Streaming class
class Streaming(PyTTaObj):
    """
    Wrapper class for SoundDevice stream-like classes. This is intended for
    applications where both measurement and analysis signal must be handled
    at runtime and/or continuously.

    Parameters:
    ------------

        * device:
            Integer or list of integers, the ID number of the desired device to
            reproduce and/or record audio data, as querried by list_devices()
            function.

        * integration:
            The integration period for SPL monitoring, given in seconds.

        * inChannels:
            List of ChannelObj for measurement channels setup

        * outChannels:
            List of ChannelObj for reproduction channels setup. This parameter
            is ignored if `excitation` is provided

        * duration:
            The amount of time that the stream will be active at each start()
            call. This parameter is ignored if `excitation` is provided.

        * excitation:
            A SignalObj used to provide outData, outChannels and samplingRate
            values.

    Attributes:
    ------------

        All parameters are also attributes, along with the ones explained here.

        * inData:
            Recorded audio data (only if `inChannels` provided).

        * outData:
            Audio data used for reproduction (only if `outChannels` provided).

        * active:
            Wrapper for stream.active attribute

        * stopped:
            Wrapper for stream.stopped attribute

        * closed:
            Wrapper for stream.closed attribute

        * stream:
            The actual SoundDevice stream-like object. More information about
            it at http://python-sounddevice.readthedocs.io/

        * durationInSamples:
            Number of recorded samples (only if `duration` provided)

        At least one channels list must be provided for the object
        initialization, either inChannels or outChannels.

    Methods:
    ---------

        * start():
            Wrapper call of stream.start() method

        * stop():
            Wrapper call of stream.stop() method

        * close():
            Wrapper call of stream.close() method

        * get_inData_as_signal():
            Returns the recorded data stored at `inData` as a SignalObj

    Class method:
    ---------------

        * __timeout(obj):
            Class caller for stopping the stream from within callback function

    Callback functions:
    --------------------

        The user can pass his/her own callback function, as long as it have the
        same structure as the ones provided by the Streaming class itself,
        with respect to the number of parameters and its application.

        * __Icallback(Idata, frames, time, status):
            Callback function used for input-only streams:

                * Idata:
                    Numpy array with input audio with `frames` length.

                * frames:
                    Number of frames read at each callback call. Same as
                    `blocksize`.

                * time:
                    Object-like with three timestamps:
                        The first sample read;
                        The last sample read;
                        The callback call.

                * status:
                    PortAudio status flag used to identify if samples were lost
                    due to last callback processing or delayed syscalls

        * __Ocallback(Odata, frames, time, status):
            Callback function used for output-only streams:

                * Odata:
                    An uninitialized Numpy array to be filled with `frames`
                    samples at each call to the callback. This parameter must
                    be full at the callback `return`, if user do not provide
                    enough samples it is filled with zeros. The values must be
                    passed to the parameter in a statement like this:

                        >>> Odata[:] = outputData[:]

                    If no subscription is made on the Odata parameter, the
                    reproduction fails.
            Other parameters are the same as the :method:`__Icallback`

        * __IOcallback(Idata, Odata, frames, time, status):
            Callback function used for input-output streams.
            It\'s parameters are the same as the previous methods.
    """

    def __init__(self, IO: str,
                 msmnt: Measurement,
                 datatype: str='float32',
                 blocksize: int=0,
                 duration: Optional[float] = 5,
                 monitor_callback: Optional[Callable] = None,
                 *args, **kwargs):
        """

        :param msmnt: PyTTa Measurement-like object.
        :type msmnt: pytta.RecMeasure
        :param datatype: string with the data type name
        :type datatype: str
        :param blocksize: number of frames reads on each call of the stream callback
        :type blocksize: int
        """
        super().__init__(*args, **kwargs)
        self._IO = IO
        self._samplingRate = msmnt.samplingRate  # registers samples per second
        self._numSamples = msmnt.numSamples  # registers total amount of samples recorded
        self._dataType = datatype  # registers data type
        self._blockSize = blocksize  # registers blocksize
        if duration is not None:
            self._durationInSamples = int(duration*msmnt.samplingRate)
        else:
            self._durationInSamples = None
        self._duration = duration
        self._device = msmnt.device
        self._theEnd = False
        self.switch = Event()
        self.switch.clear()
        self.running = Event()
        self.running.clear()
        """
        Essentially, the Event object is a boolean state. It can be
        `.set()` : Internally defines it to be True;
        `.clear()` : Internally defines it to be False;
        `.is_set()` : Check if it is True (only after call to `.set()`)

        This Event, from multiprocessing library, can be checked from different
        processes simultaneously.
        """
        self.lastStatus = None  # will register last status passed by stream
        self.queue = Queue(self.numSamples // 16)  # instantiates a multiprocessing Queue
        """
        A Queue is First In First Out (FIFO) container object. Data can be stored in it
        and be retrieved in the same order as it has been put. It can
        `.put()` : Add data to Queue
        `.put_nowait()` : Add data to Queue without waiting for memlocks
        `.get()` : Retrieve data from Queue
        `.get_nowait()` : Retrieves data from Queue without waiting for memlocks

        This Queue, from multiprocessing library, can be checked from different
        processes simultaneously.
        """
        self.set_monitoring(monitor_callback)
        self.set_io_properties(msmnt)
        return

    def __enter__(self):
        """
        Provides context functionality, the `with` keyword, e.g.

            >>> with Recorder(Measurement) as rec:  # <-- called here
            ...     rec.set_monitoring(Callable)
            ...     rec.run()
            ...
            >>>

        """
        return self

    def __exit__(self, exc_type: Type, exc_val: Exception, exc_tb: Type):
        """
        Provides context functionality, the `with` keyword, e.g.

            >>> with Streaming('play', Measurement) as strm:
            ...     strm.set_monitoring(Callable)
            ...     strm.run()
            ...                             # <-- called here
            >>>
        """
        if exc_tb:
            raise exc_val
        else:
            return

    def set_io_properties(self, msmnt):
        if 'I' in self.IO:
            self.inChannels = msmnt.inChannels
            self.recData = np.empty((self.numSamples, self.numInChannels), dtype=self.dataType)
        if 'O' in self.IO:
            self.outChannels = msmnt.outChannels
            self.playData = msmnt.excitation.timeSignal
        self.count = int()
        return

#    def play_data_adjust(self, playdata):
#        len = playdata.shape[0]
#        chn = playdata.shape[1]
#        bs = self.blockSize
#        nchunks = int(np.ceil(len / bs))
#        array = np.empty((nchunks, bs, chn), dtype='float32')
#        for c in range(chn):
#            for n in range(nchunks):
#                array[n, :, c] = playdata[n * bs:(n + 1) * bs, c]
#        return array
#
#    def rec_data_adjust(self, nsamples, nchannels):
#        bs = self.blockSize
#        nchunks = int(np.ceil(nsamples / bs))
#        array = np.empty((nchunks, bs, nchannels), dtype='float32')
#        return array
#

    def set_monitoring(self, monitor = None):
        """
        Set up the class used as monitor. It must have the following methods with these names:

            def setup(None) -> None:

                _Call any function and other object configuration needed for the monitoring_

                return


            def callback(indata: np.ndarray, outdata: np.ndarray,
                         frames: int, status: sd.CallbackFlags) -> None:

                _Process the data gathered from the stream_

                return

        It will be called from within a parallel process that the Recorder starts and
        terminates during it's .run() call.

        :param monitor: Object or class that will be used to monitor the stream data flow.
        :type monitor: object

        """
        if monitor is None:
            self.switch.clear()
            self.monitor = None
        else:
            self.switch.set()
            self.monitor = monitor
        return

    def monitoring(self):
        self.monitor.setup()
        while not self.running.is_set():
            continue
        while self.running.is_set():
            try:  # call to switch.set()
                indata, outdata, frames, status = self.queue.get_nowait()  # get from queue
                if status:  # check any status
                    self.lastStatus = status
                self.monitor.callback(indata, outdata, frames, status)  # calls for monitoring function
            except Empty:  # if queue has no data
                continue
        self.monitor.tear_down()
        return

    def runner(self, StreamType: Type, stream_callback):
        """
        Instantiates a sounddevice.InputStream and calls for a parallel process
        if any monitoring is set up.
        Then turn on the switch Event, and starts the stream.
        Waits for it to finish, unset the event
        And terminates the process

        :return:
        :rtype:
        """
        with StreamType(samplerate=self.samplingRate,
                        blocksize=self.blockSize,
                        device=self.device,
                        channels=self.numChannels,
                        dtype=self.dataType,
                        latency='low',
                        dither_off=True,
                        callback=stream_callback) as stream:
            if self.switch.is_set():
                t = Process(target=self.monitoring)
                t.start()
            self.running.set()
            stream.start()
            while stream.active:
                pass
            stream.stop()
            self.running.clear()
            if self.switch.is_set():
                t.join()
                t.close()
            self.queue.close()
        return

    def calib_pressure(self, chIndex, refPrms=1.00, refFreq=1000):
        """
        .. method:: calibPressure(chIndex, refSignalObj, refPrms, refFreq):
            use informed SignalObj, with a calibration acoustic pressure
            signal, and the reference RMS acoustic pressure to calculate the
            Correction Factor and apply to every incoming audio on specified
            channel.

            >>> Streaming.calibPressure(chIndex,refSignalObj,refPrms,refFreq)

        Parameters:
        -------------

            * chIndex (), (int):
                channel number for calibration;

            * refPrms (1.00), (float):
                the reference pressure provided by the acoustic calibrator;

            * refFreq (1000), (int):
                the reference sine frequency provided by the acoustic
                calibrator;
        """
        refSignalObj = RecMeasure(lengthDomain='time',
                                  timeLength=5,
                                  samplingRate=self.samplingRate,
                                  inChannels=chIndex,
                                  device=self.device).run()
        if chIndex-1 in self.inChannels.mapping():
            self.inChannels[chIndex-1].calib_press(refSignalObj, refPrms, refFreq)
            self.inChannels[chIndex-1].calibCheck = True
        else:
            raise IndexError('chIndex greater than channels number')
        return

    @property
    def IO(self):
        return self._IO

    @property
    def device(self):
        return self._device

    @property
    def blockSize(self):
        return self._blockSize

    @property
    def dataType(self):
        return self._dataType

    @property
    def duration(self):
        return self._durationInSamples/self.samplingRate

    @property
    def durationInSamples(self):
        return self._durationInSamples

    @property
    def numInChannels(self):
        return len(self.inChannels)

    @property
    def numOutChannels(self):
        return len(self.outChannels)

    @property
    def theEnd(self):
        return self._theEnd

    @property
    def numChannels(self):
        if self.IO == 'I':
            return self.numInChannels
        elif self.IO == 'O':
            return self.numOutChannels
        elif self.IO == 'IO':
            return self.numInChannels, self.numOutChannels


# Recording obj class
class Recorder(Streaming):
    """
    Recorder:
    ----------

        Provides a recorder object that executes, in a parallel process some function
        with the incoming data.
    """
    def __init__(self, msmnt: Measurement,
                 datatype: str='float32',
                 blocksize: int=0,
                 duration: Optional[float] = 5,
                 *args, **kwargs):
        """

        :param msmnt: PyTTa Measurement-like object.
        :type msmnt: pytta.RecMeasure
        :param datatype: string with the data type name
        :type datatype: str
        :param blocksize: number of frames reads on each call of the stream callback
        :type blocksize: int
        """
        super().__init__('I', msmnt, datatype, blocksize, duration, *args, **kwargs)
        return

    def stream_callback(self, indata: np.ndarray, frames: int,
                        times: type, status: sd.CallbackFlags):
        """
        This method will be called from the stream, as stated on sounddevice's documentation.
        """
        self.recData[self.count:frames + self.count, :] = indata[:]
        if self.monitor:
            self.queue.put_nowait((indata[:], None, frames, status))
        self.count += frames
        if self.count >= self.durationInSamples:
            raise sd.CallbackStop
        return

    def retrieve(self):
        arr = self.recData.reshape((self.numSamples, self.numInChannels))
        assert arr.ndim == 2
        signal = SignalObj(arr, 'time', self.samplingRate,
                           freqMin=20, freqMax=20e3)
        return signal

    def run(self):
        self.runner(sd.InputStream, self.stream_callback)
        return

# Playback obj class
class Player(Streaming):
    """
    Recorder:
    ----------

        Provides a recorder object that executes, in a parallel process some function
        with the incoming data.
    """
    def __init__(self, msmnt: Measurement,
                 datatype: str='float32',
                 blocksize: int=0,
                 *args, **kwargs):
        """

        :param msmnt: PyTTa Measurement-like object.
        :type msmnt: pytta.RecMeasure
        :param datatype: string with the data type name
        :type datatype: str
        :param blocksize: number of frames reads on each call of the stream callback
        :type blocksize: int
        """
        super().__init__('O', msmnt, datatype, blocksize, *args, **kwargs)
        return

    def stream_callback(self, outdata: np.ndarray, frames: int,
                        times: type, status: sd.CallbackFlags):
        """
        This method will be called from the stream, as stated on sounddevice's documentation.
        """
        outdata[:] = self.playData[self.count:frames + self.count, :]
        if self.monitor:
            self.queue.put_nowait((None, outdata[:], frames, status))
        self.count += frames
        if self.count >= self.durationInSamples:
            raise sd.CallbackStop
        return

    def run(self):
        """
        Instantiates a sounddevice.OutputStream and calls for a parallel process
        if any monitoring is set up.
        Then turn on the switch Event, and starts the stream.
        Waits for it to finish, unset the event
        And terminates the process

        :return:
        :rtype:
        """
        self.runner(sd.OutputStream, self.stream_callback)
        return


class PlaybackRecorder(Streaming):
    """
    ...
    """
    def __init__(self, msmnt: Measurement,
                 datatype: str = 'float32',
                 blocksize: int = 0):
        super().__init__('IO', msmnt, datatype, blocksize)
        return

    def stream_callback(self, indata, outdata, frames, time, status):
        try:
            outdata[:] = self.playData[self.count:frames + self.count, :]
            self.recData[self.count:frames + self.count, :] = indata[:]
            if self.monitor:
                self.queue.put_nowait((self.recData[self.count:frames + self.count, :].copy(),
                                       self.playData[self.count:frames + self.count, :].copy(),
                                       frames, status))
            self.count += frames
        except ValueError:
            raise sd.CallbackStop
        except Exception as e:
            print(type(e), e, '\n', 'Last Callback Status:', status)
            raise sd.CallbackAbort
        return

    def retrieve(self):
        signal = SignalObj(self.recData, 'time', self.samplingRate,
                           freqMin=20, freqMax=20e3)
        return signal

    def run(self):
        self.runner(sd.Stream, self.stream_callback)
        return

