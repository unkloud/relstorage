===================
 Benchmark Results
===================

Local Client Tests
==================

These tests are reported using a data size of 142MB and a cache size
of 100MB, no cache compression, a hash seed of 0 (where supported) and -O. Unless otherwise
noted, they're with Python 3.4.5.

Before any modifications (1b3910195c2b7ce666e4bd2cbecf28a79aa094b3/master)
I get these results::

  epop average 2.9002058423357084 stddev 0.04144877721013096
  mix  average 4.471249947004253 stddev 0.04006648891850737
  pop  average 3.7491709959964887 stddev 0.23962025010454424
  read average 0.6041574839910027 stddev 0.009453648365701957

With the Segmented LRU code (d030cd0984190606c2242716136c7aaaf8688fa8)
and the same settings, I get these results::

  epop average 2.4199622353335144 stddev 0.042997719276749106
  mix  average 2.5886075626670695 stddev 0.1294457218186933
  pop  average 2.1744621043326333 stddev 0.3027539534906522
  read average 0.6012111723296888 stddev 0.008994969145240476

Here's PyPy for the Segmented LRU (it's back to missing all keys for
22 groups for some reason)::

  epop average 0.688376029332 stddev 0.0117758786196
  mix  average 0.453514734904 stddev 0.0258164430069
  pop  average 0.397752523422 stddev 0.0734281091208
  read average 0.110461314519 stddev 0.0189612338017

Here's Python 2.7::

  epop average 2.29191493988 stddev 0.0770279233731
  mix  average 2.80130529404 stddev 0.331941143797
  pop  average 1.97871669134 stddev 0.0841810847293
  read average 0.489647944768 stddev 0.00126759078684

For what it's worth, it's also possible to design a trivial workload
function that generates a hit rate of 0.0 in the old code, and 100% in
the new code.

Persistence Tests
=================

Benchmark for the general approach:

Pickle is about 3x faster than marshal if we write single large
objects, surprisingly. If we stick to writing smaller objects, the
difference narrows but is still perceptible. (Naturally on PyPy the
reverse is true: pickle and zodbpickle are very slow, but marshal is
much faster. We could, but don't, take advantage of that.)

Writing 525MB (524,285,508 bytes) of data, 655K (651,065) keys (no compression):

- code as-of commit e58126a (the previous major optimizations for version 1 format)
  version 1 format, solid dict under 3.4: write: 3.8s/read 7.09s
  2.68s to update ring, 2.6s to read pickle
- in a btree under 3.4: write: 4.8s/read 8.2s
  written as single list of the items
  3.1s to load the pickle, 2.6s to update the ring
- in a dict under 3.4: write: 3.7s/read 7.6s
  written as the dict and updated into the dict
  2.7s loading the pickle, 2.9s to update the dict
- in a dict under 3.4: write: 3.0s/read 12.8s
  written by iterating the ring and writing one key/value pair
  at a time, so this is the only solution that
  automatically preserves the LRU property (and would be amenable to
  capping read based on time, and written file size); this format also lets us avoid the
  full write buffer for HIGHEST_PROTOCOL < 4
  2.5s spent in pickle.load, 8.9s spent in __setitem__,5.7s in ring.add
- in a dict: write 3.2/read 9.1s
  same as above, but custom code to set the items
  1.9s in pickle.load, 4.3s in ring.add
- same as above, but in a btree: write 2.76s/read 10.6
  1.8s in pickle.load, 3.8s in ring.add,

For the final version with optimizations (file format two), the write
time is 2.3s/read is 6.4s.

Segmented LRU
-------------

With the code before any modifications
(1b3910195c2b7ce666e4bd2cbecf28a79aa094b3) and using the same
benchmark framework along with PYTHONHASHSEED=0, we load and store
650987 objects. The write time is 1.7s and the read time is 2.4s; the
total benchmark results (number=4, repeat_count=3) are::

  read  average 8.927879446661487 stddev 0.03242392820916275
  write average 5.86237387000195 stddev 0.025450127071328835

With the code fully implementing segmented LRU
(cb663604a969ad894c74d6fc06fa47fd3be49f94), PYTHONHASHSEED=0, number=4
repeat=3 the write time is 2.5s and the read time is 2.3s. Full
benchmark results::

  read  average 6.9280044683255255 stddev 0.07165229299434527
  write average 7.996576041332446 stddev 0.05586695632417015

.. note:: In this version, even though there are 651,065 objects for a
          total size of 524,285,508, we're only loading/storing
          521,182 of them (because we're only filling the protected
          space).

When we stop aging an write and limit simply by byte count, and start
flowing items through eden, not just the protected ring, our write
time goes back to about 2.6s. Our read time increased substantially,
so we added a bulk method in C, giving us times once again comparable::

  read  average 6.240402834334721 stddev 0.5385303523379349
  write average 7.7160701316703735 stddev 0.505427296067659

A little work on optimization of writing (limiting CFFI attribute
access) gets us to these numbers, which are both faster than
the original numbers::

  read  average 6.409313925498282 stddev 0.18588680639842908
  write average 5.651123669245862 stddev 0.023198867394568865

With commit d844311078079a3e203883b5e1e0dbac4e385b81 on Python 3.7.3::

  write: Mean +- std dev: 2.09 sec +- 0.06 sec
  read : Mean +- std dev: 3.83 sec +- 0.11 sec

pyperf's tracemalloc reports that write uses 129.5MB (probably for the
pickler memo cache) and read uses 816.7MB; using psutil to check the USS
around the read loop shows a change of 1200 MB (1,250,291,712) for the
first iteration and 841 MB (882,823,168) for the second.

When the code is modified to use (oid_int, tid_int) tuples for keys
and (state_bytes, tid_int) tuples for values, we get times that are
surprisingly somewhat worse::

  write: Mean +- std dev: 2.30 sec +- 0.05 sec
  read: Mean +- std dev: 4.22 sec +- 0.12 sec

The cache contains 619,735 keys using 500MB (524,998,312) of data
(``cache_local_mb = 525``). Writing it reports 165.0 MB of memory used
(turning on ``pickler.fast`` to disable the memo reduces this to
52.4MB). Reading reports 877.2MB used (but if ``pickler.fast`` was set
to True on writing, this goes up to 1509 MB), with a delta USS change
of 251MB (264,032,256).

Turning ``pickler.fast`` on, we get::

  write stream: Mean +- std dev: 1.55 sec +- 0.06 sec
  read stream: Mean +- std dev: 3.66 sec +- 0.24 sec

Reading reports a delta USS of 944MB (989,327,360) for the first
iteration, and 371MB (388,587,520) for the second. The third
iteration, strangely, shows 741MB (776,749,056).

At the level of the local client, which uses sqlite, for this same
data, we take::

  write client fresh: Mean +- std dev: 7.82 sec +- 0.10 sec
  write client dups: Mean +- std dev: 2.02 sec +- 0.14 sec
  read client: Mean +- std dev: 6.79 sec +- 0.17 sec

The allocation and USS patterns are very close to the same as for
reading the stream. We spend 4.3s to put rows in the temp table when
the file doesn't exist, and 1.9 seconds to do so when we don't
actually need to put any items in for the dups case.

This is slower, but enables a much better caching experience. The file
size on disk is 732,467,200 to store 524,287,908 bytes in memory.
That's a supremely large cache. A more reasonable 50mb cache gets us::

  write stream: Mean +- std dev: 166 ms +- 0 ms
  read stream: Mean +- std dev: 411 ms +- 5 ms
  write client fresh: Mean +- std dev: 1.09 sec +- 0.01 sec
  write client dups: Mean +- std dev: 185 ms +- 4 ms
  read client: Mean +- std dev: 548 ms +- 2 ms

  write stream: 5232.9 kB
  read stream: 91.6 MB
  write client fresh: 9242.2 kB
  write client dups: 9475.8 kB
  read client: 91.0 MB

Simulations
===========

ASU is the application identifier. Here, we will treat that like a
connection.

There are two distinct datasets. One is based on storage traces
(http://traces.cs.umass.edu/index.php/Storage/Storage):

==========  ========== ======== =========  ========== ====
   File     Operations    Keys    Reads      Writes   ASUs
==========  ========== ======== =========  ========== ====
Financial1   5,334,987  710,908 1,235,633   4,099,354   24
Financial2   3,699,194  296,072 3,046,112     653,082   19
WebSearch1   1,055,448  480,446 1,055,236         212    6
WebSearch2   4,579,809  726,501 4,578,819         990    6
WebSearch3   4,261,709  707,802 4,260,449        1260    9
==========  ========== ======== =========  ========== ====

The other is based on caches used for an ORM and HTTP system
(https://github.com/cache2k/cache2k-benchmark). It does not include
the read/write distinction or the size of the requests, so we choose
those as additional parameters. Here, we used a 30% write ratio and a
mean object size of 8192 bytes with a standard deviation of 512. We
also arbitrarily choose the number of connections to be 8.

==========  ========== ======== =========  ========== ====
   File     Operations    Keys    Reads      Writes   ASUs
==========  ========== ======== =========  ========== ====
orm-busy     5,000,000   76,349 3,500,000   1,500,000   8
orm-night    5,000,000   86,466 3,500,000   1,500,000   8
web07           76,118   20,484    53,283      22,835   8
web12           95,607   13,756    66,925      28,682   8
==========  ========== ======== =========  ========== ====

Note that Financial1 and Financial2 are OLTP traces of a journal file,
and orm-busy and orm-night are traces of an ORM session cache with
short transactions. Both of these are dominated by *recency* and are
thus very easy for LRU caches; a frequency cache like the new code has
more trouble with them at smaller sizes. They are included to
demonstrate worst-case performance and are probably not representative
of typical RelStorage cache workloads (a RelStorage workload will have
some objects, such as catalog BTree objects, that are frequently
accessed which shouldn't be ejected if a more rare query occurs).
The hit rates of these workloads are strongly correlated to the size
of the eden generation.

Cache simulation
----------------

This works at the raw, low level if the recently used lists. It
doesn't incorporate any notion of connections or transactions, and it
doesn't know anything about key checkpoints.

* Storage Traces

============  ==========  =========  =========  ========  =========
 File         Cache Size   Hits LRU  Hits SLRU  Time LRU  Time SLRU
============  ==========  =========  =========  ========  =========
Financial1      100           0.716      0.664      40.1     36.09  X
Financial1      512           0.839      0.826      37.7     29.64  X
Financial1     1024           0.881      0.893      36.3     28.82
Financial2      100           0.851      0.847      21.3     17.64  X
Financial2      512           0.920      0.920      18.8     17.46
Financial2     1024           0.921      0.921      18.0     17.68
WebSearch1      100           0.007      0.023      12.1      8.72
WebSearch1      512           0.042      0.120      11.8      8.16
WebSearch1     1024           0.187      0.223      11.5      7.88
WebSearch2      100           0.007      0.029      51.5     39.84
WebSearch2      512           0.044      0.146      52.5     38.99
WebSearch2     1024           0.214      0.271      46.0     35.00
WebSearch3      100           0.007      0.029      46.4     36.52
WebSearch3      512           0.048      0.147      50.1     36.41
WebSearch3     1024           0.222      0.279      42.9     32.09
============  ==========  =========  =========  ========  =========

* Cache Traces

Most of these results were similar or identical given the small size
of the data. Only tests that show a difference are reported. In 8
cases the results were identical, in the remaining four they each one two.

SLRU

==============  ===== ======= ======= =====
File            Limit    Size    Time  Hits
==============  ===== ======= ======= =====
orm-busy          100   95.36   25.63 0.909
orm-busy          512  488.27   24.68 0.980
orm-night         100   95.34   27.90 0.928
web07             100   95.36    0.49 0.683
==============  ===== ======= ======= =====

LRU


==============  ===== ======= ======= =====
File            Limit    Size    Time  Hits
==============  ===== ======= ======= =====
orm-busy          100   95.36   26.99 0.895
orm-busy          512  488.28   23.69 0.978
orm-night         100   95.36   25.33 0.941
web07             100   95.37    0.59 0.689
==============  ===== ======= ======= =====


Storage Simulation
------------------

Compared to the above, this operates at the same level as the actual
``StorageCache``. Operations are divided by connection, and keys are
checkpointed at regular intervals (here, 10,000 changes, the default).
Connections only poll for changes periodically to simulate
transactions (here, after every 10 operations, or if there would be a
read conflict.)

* Storage Traces

SLRU f8890082770af24c08a0656579fd6d3bd77e2658

==============  ===== ======= ======= =====
File            Limit    Size    Time  Hits
==============  ===== ======= ======= =====
Financial1.spc    100   95.49  184.22 0.715
Financial1.spc    512  495.55  204.71 0.767
Financial1.spc   1024  980.42  195.23 0.780
Financial2.spc    100   96.55   64.12 0.477
Financial2.spc    512  493.01   63.53 0.665
Financial2.spc   1024  980.09   61.54 0.731
WebSearch1.spc    100   95.52   13.71 0.023
WebSearch1.spc    512  488.44   13.87 0.117
WebSearch1.spc   1024  976.72   13.23 0.216
WebSearch2.spc    100   95.37   62.54 0.030
WebSearch2.spc    512  488.27   62.94 0.143
WebSearch2.spc   1024  976.55   57.40 0.265
WebSearch3.spc    100   95.36   58.12 0.030
WebSearch3.spc    512  488.27   57.85 0.145
WebSearch3.spc   1024  976.55   52.62 0.269
==============  ===== ======= ======= =====

LRU/master XXX -> The time numbers are preliminary

==============  ===== ======= ======= =====
File            Limit    Size    Time  Hits
==============  ===== ======= ======= =====
Financial1.spc    100   95.36  226.75 0.779 X
Financial1.spc    512  488.28  232.16 0.781 X
Financial1.spc   1024  976.55  222.40 0.781 X
Financial2.spc    100   95.37   71.05 0.712 X
Financial2.spc    512  488.28   71.47 0.751 X
Financial2.spc   1024  976.56   71.89 0.751 X
WebSearch1.spc    100   95.37   17.38 0.008
WebSearch1.spc    512  488.28   16.85 0.043
WebSearch1.spc   1024  976.56   15.14 0.188
WebSearch2.spc    100   95.37   73.49 0.008
WebSearch2.spc    512  488.28   73.87 0.046
WebSearch2.spc   1024  976.56   66.59 0.213
WebSearch3.spc    100   95.37   68.66 0.008
WebSearch3.spc    512  488.27   68.02 0.051
WebSearch3.spc   1024  976.55   60.87 0.222
==============  ===== ======= ======= =====

We can see that the write heavy operations perform somewhat worse in
the SLRU scheme. The worst case scenario is Financial2 with a cache
size of 100 MB; simple LRU gets a hit ratio that's .23 better. On the
plus side, the new code is at least faster than the old code.

If we triple the ``cache_delta_size_limit`` to 30000, then SLRU does
substantially better:


==============  ===== ======= ======= =====
File            Limit    Size    Time  Hits
==============  ===== ======= ======= =====
Financial1.spc    100   95.87  194.94 0.730 X
Financial1.spc    512  496.59  204.31 0.773 X
Financial1.spc   1024  980.41  215.15 0.801
Financial2.spc    100  100.13   67.01 0.551 X
Financial2.spc    512  496.51   63.53 0.707 X
Financial2.spc   1024  977.41   64.95 0.776
==============  ===== ======= ======= =====

* Cache Traces

SLRU


==============  ===== ======= ======= =====
File            Limit    Size    Time  Hits
==============  ===== ======= ======= =====
orm-busy          100   95.37  104.43 0.699
orm-busy          512  488.36  105.70 0.739
orm-busy         1024  976.63  102.10 0.757
orm-night         100   95.39  102.70 0.649
orm-night         512  488.43  104.88 0.739
orm-night        1024  976.73  104.25 0.797
web07             100   95.39    1.67 0.688
web07             512  355.72    1.59 0.796
web07            1024  355.72    1.64 0.796
web12             100   95.40    1.95 0.781
web12             512  366.35    1.91 0.891
web12            1024  366.35    1.86 0.891
==============  ===== ======= ======= =====

LRU


==============  ===== ======= ======= =====
File            Limit    Size    Time  Hits
==============  ===== ======= ======= =====
orm-busy          100   95.36  117.10 0.750
orm-busy          512  488.28  117.71 0.802
orm-busy         1024  976.56  120.82 0.826
orm-night         100   95.36  110.87 0.789
orm-night         512  488.27  109.66 0.838
orm-night        1024  976.56  104.90 0.868
web07             100   95.36    1.79 0.739
web07             512  355.72    1.51 0.796
web07            1024  355.72    1.49 0.796
web12             100   95.36    2.00 0.856
web12             512  366.35    1.77 0.891
web12            1024  366.35    1.77 0.891
==============  ===== ======= ======= =====

Before Shared Memory
====================

Before moving to shared memory, I took some fresh benchmarks with just
the local cache: commit eb1b5e23ecf832aeeb2ec2febec46658758560e8 and
Python 3.8.7, macOS 10.15. Default settings::

  pop_bulk: Mean +- std dev:  43.4 ms +- 1.0 ms
  pop_eq  : Mean +- std dev: 162   ms +- 6 ms
  pop_ne  : Mean +- std dev: 278   ms +- 15 ms
  epop    : Mean +- std dev: 159   ms +- 4 ms

The 'read' test was taking over four hours to do just 16 main
iterations. (Reporting a hit ratio of 0.5591147132169576.) Turning it
way down (with options  -p3 --values 3 -l 10), the entire run looks
like::

  pop_bulk: Mean +- std dev:  47.2 ms +- 0.9 ms
  pop_eq  : Mean +- std dev: 210   ms +- 6 ms
  pop_ne  : Mean +- std dev: 364   ms +- 14 ms
  epop    : Mean +- std dev: 175   ms +- 3 ms
  read    : Mean +- std dev:  10.3 ms +- 0.5 ms
  mix     : Mean +- std dev: 368   ms +- 9 ms

Interprocess Recursive Mutex
----------------------------

Adding a ``interprocess_recursive_mutex`` that is never contended
results in these timings::

  pop_bulk: Mean +- std dev:  49.1  ms +- 1.2 ms
  pop_eq  : Mean +- std dev: 202    ms +- 3 ms
  pop_ne  : Mean +- std dev: 360    ms +- 6 ms
  epop    : Mean +- std dev: 171    ms +- 3 ms
  read    : Mean +- std dev:   9.88 ms +- 0.28 ms
  mix     : Mean +- std dev: 340    ms +- 12 ms

Contended Results
-----------------

Before any modifications, but after the addition of the contended
tests for 'read' and mix::

  pop_bulk : Mean +- std dev:   43.7  ms +- 0.9 ms
  pop_eq   : Mean +- std dev:  183    ms +- 6 ms
  pop_ne   : Mean +- std dev:  357    ms +- 27 ms
  epop     : Mean +- std dev:  163    ms +- 5 ms
  read     : Mean +- std dev:    9.43 ms +- 0.18 ms
  read_cont: Mean +- std dev:  198    ms +- 17 ms
  mix      : Mean +- std dev:  326    ms +- 20 ms
  mix_cont : Mean +- std dev: 8620    ms +- 0.20 sec

With the ``interprocess_recursive_mutex``, as above, and some fairly
fine-grained ``nogil`` statements directly around calls to the cache,
the read and mixed contended tests are about 10x worse; the good news,
though, is that the CPU usage actually went to 200% indicating actuall
parallelism::

  pop_bulk : Mean +- std dev:    47.3 ms +- 1.5 ms
  pop_eq   : Mean +- std dev:   202   ms +- 2 ms
  pop_ne   : Mean +- std dev:   362   ms +- 8 ms
  epop     : Mean +- std dev:   172   ms +- 2 ms
  read     : Mean +- std dev:    10.8 ms +- 0.5 ms
  read_cont: Mean +- std dev:  2000   ms +- 0.14 sec
  mix      : Mean +- std dev:   370   ms +- 31 ms
  mix_cont : Mean +- std dev: 70700   ms +- 1.7 sec

Just benchmarking taking the lock (whether recursive or not made no
difference), it looks like it's releasing/acquiring the GIL that hurts
us. Here, the 'gil' benchmarks don't actually get any concurrency
(even in the 'cont' -> contested case, where we use threads; the other
cases are single threaded) because the GIL is held for the entire
duration of the call into Cython -> C++; the nogil versions, on the
other hand, get concurrency by releasing the GIL when calling into
C++. But acquiring the lock, even when contended...is faster than
dropping/releasing the GIL? ::

  lock_nogil     : Mean +- std dev:   1240 us +- 0.03 ms
  lock_gil       : Mean +- std dev:    578 us +- 14 us

  lock_nogil_cont: Mean +- std dev: 225000 us +- 4 ms
  lock_gil_cont  : Mean +- std dev:   2030 ms +- 0.22 ms

The single threaded nogil case is 2.2x slower than the gil case. So
that gives us a basic overhead.

The multi-threaded case (5 threads) is 110x slower.

Here's two threads::

  lock_nogil     : Mean +- std dev: 1260 us +- 0.05 ms
  lock_gil       : Mean +- std dev:  586 us +- 19 us
  lock_nogil_cont: Mean +- std dev: 9700 us +- 0.79 ms
  lock_gil_cont  : Mean +- std dev: 1130 us +- 0.07 ms

Same ratio for single threaded. For multi-threaded, it's 8.5x.

Three threads goes to 44x, and four goes to 67x.

Python 2.7 does better, but Python 3.9 is basically the same in both
ratio and absolute times as 3.8::

     Ratio     Python 3.8   Python 2.7                Python 3.9
     Threads
         1           2.2         1.5 (1.39ms/.892ms)      2.07
         2           8.5         4.6 (11.1ms/2.4ms)       9.27
         3          44          14   (55.7ms/4.0ms)      41
         4          67          19   (110ms/5.5ms)       67
         5         110          29   (193ms/6.6ms)      108

Adding tests that do nothing but call a trivial constant-time
function. Here's the raw data for Python 3.9::

  trivial_nogil1: Mean +- std dev: 1.09 ms +- 0.07 ms
  trivial_gil1  : Mean +- std dev: 362 us +- 27 us
  lock_nogil1   : Mean +- std dev: 1.31 ms +- 0.08 ms
  lock_gil1     : Mean +- std dev: 565 us +- 47 us
  trivial_nogil2: Mean +- std dev: 7.31 ms +- 0.90 ms
  trivial_gil2  : Mean +- std dev: 651 us +- 41 us
  lock_nogil2   : Mean +- std dev: 10.4 ms +- 0.7 ms
  lock_gil2     : Mean +- std dev: 1.10 ms +- 0.10 ms
  trivial_nogil3: Mean +- std dev: 55.5 ms +- 3.2 ms
  trivial_gil3  : Mean +- std dev: 817 us +- 54 us
  lock_nogil3   : Mean +- std dev: 69.8 ms +- 2.8 ms
  lock_gil3     : Mean +- std dev: 1.50 ms +- 0.12 ms
  trivial_nogil4: Mean +- std dev: 104 ms +- 4 ms
  trivial_gil4  : Mean +- std dev: 1.01 ms +- 0.10 ms
  lock_nogil4   : Mean +- std dev: 122 ms +- 5 ms
  lock_gil4     : Mean +- std dev: 1.74 ms +- 0.26 ms
  trivial_nogil5: Mean +- std dev: 207 ms +- 8 ms
  trivial_gil5  : Mean +- std dev: 1.13 ms +- 0.14 ms
  lock_nogil5   : Mean +- std dev: 220 ms +- 6 ms
  lock_gil5     : Mean +- std dev: 2.28 ms +- 0.22 ms

Here's the slowdown factor between dropping the GIL and not dropping
it. You can see that the trivial function scales very badly,
indicating that acquiring/releasing the GIL is quite expensive,
relatively speaking. The more expensive lock acquisition comes out
looking relatively well.

+------------+--------+-----+
| Concurrency| Trivial| Lock|
|            |        |     |
+============+========+=====+
| 1          | 3.01   | 2.31|
+------------+--------+-----+
|2           | 11.22  | 9.45|
+------------+--------+-----+
|3           | 67.9   | 46.5|
+------------+--------+-----+
|4           | 102.9  | 70.1|
+------------+--------+-----+
|5           | 183.2  | 96.5|
+------------+--------+-----+

Now we can look at the factor differences between the trivial and lock
cases to determine the relative expense of acquiring the lock. This
table shows the factor by which acquiring the lock is more expensive
than the trivial function.

+-----------+------+-----+
|Concurrency| nogil| gil |
+-----------+------+-----+
|1          | 1.2  | 1.6 |
+-----------+------+-----+
|2          | 1.4  | 1.7 |
+-----------+------+-----+
|3          | 1.3  | 1.8 |
+-----------+------+-----+
|4          | 1.2  | 1.7 |
+-----------+------+-----+
|5          | 1.1  | 2.0 |
+-----------+------+-----+

Here's the raw data for Python 2.7::

  trivial_nogil1: Mean +- std dev: 944 us +- 38 us
  trivial_gil1  : Mean +- std dev: 485 us +- 11 us
  lock_nogil1   : Mean +- std dev: 1.16 ms +- 0.03 ms
  lock_gil1     : Mean +- std dev: 680 us +- 15 us
  trivial_nogil2: Mean +- std dev: 6.71 ms +- 1.09 ms
  trivial_gil2  : Mean +- std dev: 1.40 ms +- 0.08 ms
  lock_nogil2   : Mean +- std dev: 8.35 ms +- 1.18 ms
  lock_gil2     : Mean +- std dev: 1.84 ms +- 0.13 ms
  trivial_nogil3: Mean +- std dev: 40.0 ms +- 1.8 ms
  trivial_gil3  : Mean +- std dev: 3.70 ms +- 0.26 ms
  lock_nogil3   : Mean +- std dev: 45.5 ms +- 1.9 ms
  lock_gil3     : Mean +- std dev: 2.98 ms +- 0.17 ms
  trivial_nogil4: Mean +- std dev: 82.0 ms +- 4.8 ms
  trivial_gil4  : Mean +- std dev: 4.97 ms +- 0.38 ms
  lock_nogil4   : Mean +- std dev: 87.0 ms +- 4.9 ms
  lock_gil4     : Mean +- std dev: 3.85 ms +- 0.20 ms
  trivial_nogil5: Mean +- std dev: 163 ms +- 9 ms
  trivial_gil5  : Mean +- std dev: 6.34 ms +- 0.29 ms
  lock_nogil5   : Mean +- std dev: 173 ms +- 6 ms
  lock_gil5     : Mean +- std dev: 4.69 ms +- 0.24 ms

It shows roughly the same pattern, albeit more so: the GIL is less
expensive. Interestingly, for reasons I can't explain, starting with
concurrency 4, the locked versions are *cheaper* than the trivial
versions. I *think* that might mean that the GIL scales non-linearly
on Python 2?

Tentative conclusion: locking isn't all that expensive, especially
compared to the GIL. We should only drop the GIL when we have a lot of
work to do. However, because multiple processes are involved, and
there are no fairness guarantees around the lock, we should probably
drop it before acquiring the lock; so we need to do more work in C++.

.. note:: This applies only to a single process. I cannot yet
          benchmark acquiring the same lock across processes.
