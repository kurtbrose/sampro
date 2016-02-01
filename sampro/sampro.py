'''
Data is kept in two dictionaries:
1- rooted_leaf_counts; this is the amount of samples on each function/line number pair
this data can be used to answer the question "where are my threads spending their time?"
this is useful for finding hotspots; these are namespaced by root function, this is a
decent proxy for thread

Because there is at most one leaf sample integer per line of code x thread pair, this data
structure is unbounded.

2- stack_counts; this is a count of samples in each unique stack
this data gives a more detailed view, since each unique call stack
has an independent count.

Because there may be combinatorially many unique stacks, this data structure
is bounded.  If the data structure overflows, a count of skipped stack samples
is kept.
'''
import sys
import threading
import signal
import collections
import time
import random


# A given call chain can be represented as a list of 2-tuples:
#  [ (code object, line no), (code_object, line no) ... ]

# In particular for a sampling profiler, we are interested in
# seeing which "call patterns" are hot, so the representation used will be:

# { (caller code object, caller line no, callee code object) : count }

class _BaseSampler(object):
    '''
    A Sampler that will periodically sample the running stacks of all Python threads.
    '''
    def __init__(self):
        self.rooted_leaf_counts = collections.defaultdict(lambda: collections.defaultdict(int))
        self.stack_counts = {}
        self.max_stacks = 10000
        self.skipped_stack_samples = 0
        self.sample_count = 0  # convenience for calculating percentages

    def sample(self):
        self.sample_count += 1
        sampler_frame = sys._getframe()
        cur_samples = []
        for thread_id, frame in sys._current_frames().items():
            if frame is sampler_frame:
                continue
            stack = []
            cur = frame
            while cur:
                stack.extend((cur.f_code, cur.f_lineno))
                cur, last = cur.f_back, cur
            self.rooted_leaf_counts[last.f_code][(frame.f_code, frame.f_lineno)] += 1
            stack = tuple(stack)
            if stack not in self.stack_counts:
                if len(self.stack_counts) > self.max_stacks:
                    self.skipped_stack_samples += 1
                self.stack_counts[stack] = 1
            else:
                self.stack_counts[stack] += 1

    def live_data_copy(self):
        rooted_leaf_counts = {}
        for k, v in self.rooted_leaf_counts.items():
            rooted_leaf_counts[k] = dict(v)
        return rooted_leaf_counts, dict(self.stack_counts)

    def rooted_samples_by_file(self):
        '''
        Get sample counts by file, and root thread function.
        (Useful for answering quesitons like "what modules are hot?")
        '''
        rooted_leaf_samples, _ = self.live_data_copy()
        rooted_file_samples = {}
        for root, counts in rooted_leaf_samples.items():
            cur = {}
            for key, count in counts.items():
                code, lineno = key
                cur.setdefault(code.co_filename, 0)
                cur[code.co_filename] += count
            rooted_file_samples[root] = cur
        return rooted_file_samples

    def rooted_samples_by_line(self, filename):
        '''
        Get sample counts by line, and root thread function.
        (For one file, specified as a parameter.)
        This is useful for generating "side-by-side" views of
        source code and samples.
        '''
        rooted_leaf_samples, _ = self.live_data_copy()
        rooted_line_samples = {}
        for root, counts in rooted_leaf_samples.items():
            cur = {}
            for key, count in counts.items():
                code, lineno = key
                if code.co_filename != filename:
                    continue
                cur[lineno] = count
            rooted_line_samples[root] = cur
        return rooted_line_samples

    def hotspots(self):
        '''
        Get lines sampled accross all threads, in order
        from most to least sampled.
        '''
        rooted_leaf_samples, _ = self.live_data_copy()
        line_samples = {}
        for _, counts in rooted_leaf_samples.items():
            for key, count in counts.items():
                line_samples.setdefault(key, 0)
                line_samples[key] += count
        return sorted(
            line_samples.items(), key=lambda v: v[1], reverse=True)

    def flame_map(self):
        '''
        return sampled stacks in form suitable for inclusion in a
        flame graph (https://github.com/brendangregg/FlameGraph)
        '''
        flame_map = {}
        _, stack_counts = self.live_data_copy()
        for stack, count in stack_counts.items():
            root = stack[-2].co_name
            stack_elements = []
            for i in range(len(stack)):
                if type(stack[i]) in (int, long):
                    continue
                code = stack[i]
                stack_elements.append("{0}`{1}`{2}".format(
                    root, code.co_filename, code.co_name))
            flame_key = ';'.join(stack_elements)
            flame_map.setdefault(flame_key, 0)
            flame_map[flame_key] += count
        return flame_map

    def start(self):
        raise NotImplemented()

    def stop(self):
        raise NotImplemented()


class ThreadedSampler(_BaseSampler):
    '''
    This implementation relies on a thread and sleep.
    '''
    def __init__(self):
        super(ThreadedSampler, self).__init__()
        self.stopping = threading.Event()
        self.data_lock = threading.Lock()
        self.thread = None
        self.started = False

    def start(self):
        'start a background thread that will sample ~50x per second'
        if self.started:
            raise ValueError("Sampler.start() may only be called once")
        self.started = True
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.stopping.set()

    def _run(self):
        while not self.stopping.wait(0.01 * (1 + random.random())):
            self.sample()
            # sample 50x per second
            # NOTE: sleep for a random amount of time to avoid syncing with
            # other processes (e.g. if another thread is doing something at 
            # a regular interval, we may always catch that process at the
            # same point in its cycle)


Sampler = ThreadedSampler


if hasattr(signal, "setitimer"):
    class SignalSampler(_BaseSampler):
        def __init__(self, which="real"):
            '''
            Gather performance samples ~50x / second using interrupt timers.
            One of "real", "virtual", or "prof".
            From setitimer man page, meaning of these values:
                real: decrements in real time, and delivers SIGALRM upon expiration.
                virtual: decrements only when the process is executing, and
                         delivers SIGVTALRM upon expiration.
                prof: decrements both when the process executes and when the
                      system is executing on behalf of the process.  Coupled
                      with ITIMER_VIRTUAL, this timer is usually used to
                      profile the time spent by the application in user and
                      kernel space.  SIGPROF is delivered upon expiration.

            Note that signals are inherently global for the entire process.
            '''
            super(SignalSampler, self).__init__()
            try:
                _which = {
                    'real': signal.ITIMER_REAL,
                    'virtual': signal.ITIMER_VIRTUAL,
                    'prof': signal.ITIMER_PROF
                }[which]
            except KeyError:
                raise ValueError("which must be one of 'real', 'virtual', or 'prof'"
                    " (got {0})".format(repr(which)))
            self.which = _which
            self.signal = {
                signal.ITIMER_REAL: signal.SIGALRM,
                signal.ITIMER_VIRTUAL: signal.SIGVTALRM,
                signal.ITIMER_PROF: signal.SIGPROF}[self.which]
            if signal.getsignal(self.signal) not in (None, signal.SIG_DFL, signal.SIG_IGN):
                raise EnvironmentError("handler already attached for signal")
            self.started = False
            self.stopping = False

        def  start(self):
            'start sampling interrupts'
            if self.started:
                return
            self.started = True
            self.rollback = signal.signal(self.signal, self._resample)
            signal.setitimer(self.which, 0.01 * (1 + random.random()))

        def stop(self):
            if not self.started:
                return
            self.stopping = True
            signal.setitimer(self.which, 0)
            signal.signal(self.signal, self.rollback)

        def _resample(self, signum, frame):
            if self.stopping:
                return
            self.sample()
            signal.setitimer(self.which, 0.01 * (1 + random.random()))


    Sampler = SignalSampler
