CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -fPIC

# macOS builds a .dylib; Linux (containers) builds a .so.
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  LIBEXT := dylib
  SHARED := -dynamiclib -install_name @rpath/libdfenhance.dylib
else
  LIBEXT := so
  SHARED := -shared
endif

libdfenhance.$(LIBEXT): df_enhance.c df_enhance.h
	$(CC) $(CFLAGS) $(SHARED) -o $@ df_enhance.c -lm

.PHONY: clean
clean:
	rm -f libdfenhance.dylib libdfenhance.so
