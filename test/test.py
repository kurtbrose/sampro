import sys
import os.path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/..')

import sampro


def test():
    samplers = [sampro.Sampler(), sampro.ThreadedSampler()]
    if hasattr(sampro, "SingalSampler"):
        samplers.append(sampro.SignalSampler())
    for s in samplers:
        s.start()
    work(7)
    for s in samplers:
        s.stop()
    for s in samplers:
        assert s.sample_count, "sampler {0} didn't run".format(repr(s))
        s.live_data_copy()
        files = s.rooted_samples_by_file().keys()
        for f in files:
            s.rooted_samples_by_line(f)
        s.hotspots()
        s.flame_map()


import time


def work(n):
    if not n:
        return
    time.sleep(0.01)
    work(n - 1)
    work(n - 1)


if __name__ == "__main__":
    test()
