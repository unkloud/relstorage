This is a partial copy of boost 1.71.0.

The "intrusive" directory was copied in its entirety, even though, at
this writing, we only use part of it.

The remaining portions are included because intrusive uses them. 'gcc
-H' can be used to generate a header include tree in order to shake
out unnecessary parts of these includes. Remember to support multiple
platforms::

    gcc -H src/relstorage/cache/c_cache.cpp -I include/ -I path/to/python -o /tmp/foo.o -c -DWIN32 -D_MSC_VER 2>&1 | grep boost
