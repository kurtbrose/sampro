import sys
import threading
import collections
import time


# A given call chain can be represented as a list of 2-tuples:
#  [ (code object, line no), (code_object, line no) ... ]

# In particular for a sampling profiler, we are interested in
# seeing which "call patterns" are hot, so the representation used will be:

# { (caller code object, caller line no, callee code object) : count }

class Sampler(object):
    '''
    A Sampler that can be periodically told to sample all running processes
    '''
    def __init__(self):
        self.call_count_map = collections.defaultdict(int)
        self.stopping = False
        self.started = False
        self.thread = None
        self.data_lock = threading.Lock()

    def sample(self):
        call_count_map = self.call_count_map  # eliminate attribute access
        self.data_lock.acquire()
        try:
            sampler_frame = sys._getframe()
            for frame in sys._current_frames().values():
                if frame is sampler_frame:
                    continue
                prev_code = frame.f_code
                call_count_map[(prev_code, frame.f_lineno, None)] += 1
                cur = frame.f_back

                while cur:
                    cur_code = cur.f_code
                    call_count_map[(cur_code, cur.f_lineno, prev_code)] += 1
                    cur = cur.f_back
                    prev_code = cur_code
        finally:
            self.data_lock.release()

    def live_data_copy(self):
        data = {}
        self.data_lock.acquire()
        data.update(self.call_count_map)
        self.data_lock.release()
        return data

    def start(self):
        'start a background thread that will sample ~100x per second'
        if self.started:
            raise ValueError("Sampler.start() may only be called once")
        self.started = True
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.stopping = True

    def _run(self):
        while not self.stopping:
            self.sample()
            time.sleep(0.010)  # sample 100x per second (-ish)


# TODO: 1- convenience functions to provide the current sampling in a variety of methods.
# TODO: 2- figure out a good way to msak out "idle" methods?  e.g. should a thread
# in a sleep() count the same as a thread in a tight CPU-heavy loop?
# perhaps IO bound and CPU bound should be tracked separately
