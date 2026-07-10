CC ?= clang
CFLAGS ?= -O2 -Wall -Wextra -fPIC

libuvad.dylib: uvad.c uvad.h
	$(CC) $(CFLAGS) -dynamiclib -install_name @rpath/libuvad.dylib -o $@ uvad.c

.PHONY: clean
clean:
	rm -f libuvad.dylib
