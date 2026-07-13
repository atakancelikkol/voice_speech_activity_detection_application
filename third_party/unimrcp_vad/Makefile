CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -fPIC

# macOS builds a .dylib; Linux (containers) builds a .so.
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  LIBEXT := dylib
  SHARED := -dynamiclib -install_name @rpath/libuvad.dylib
else
  LIBEXT := so
  SHARED := -shared
endif

libuvad.$(LIBEXT): uvad.c uvad.h
	$(CC) $(CFLAGS) $(SHARED) -o $@ uvad.c

.PHONY: clean
clean:
	rm -f libuvad.dylib libuvad.so
