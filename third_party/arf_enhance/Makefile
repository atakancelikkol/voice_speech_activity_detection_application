CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -fPIC

# macOS builds a .dylib; Linux (containers) builds a .so.
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  LIBEXT := dylib
  SHARED := -dynamiclib -install_name @rpath/libarfenhance.dylib
else
  LIBEXT := so
  SHARED := -shared
endif

libarfenhance.$(LIBEXT): arf_enhance.c arf_enhance.h
	$(CC) $(CFLAGS) $(SHARED) -o $@ arf_enhance.c -lm

.PHONY: clean
clean:
	rm -f libarfenhance.dylib libarfenhance.so
