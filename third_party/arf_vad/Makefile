CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -fPIC

# macOS builds a .dylib; Linux (containers) builds a .so.
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  LIBEXT := dylib
  SHARED := -dynamiclib -install_name @rpath/libarfvad.dylib
else
  LIBEXT := so
  SHARED := -shared
endif

libarfvad.$(LIBEXT): arf_vad.c arf_vad.h
	$(CC) $(CFLAGS) $(SHARED) -o $@ arf_vad.c

.PHONY: clean
clean:
	rm -f libarfvad.dylib libarfvad.so
