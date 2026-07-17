CC ?= cc
CFLAGS ?= -O2 -Wall -fPIC -Irnnoise -Irnnoise/include

# macOS builds a .dylib; Linux (containers) builds a .so.
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  LIBEXT := dylib
  SHARED := -dynamiclib -install_name @rpath/librnenhance.dylib
else
  LIBEXT := so
  SHARED := -shared
endif

RNNOISE_SRC = \
	rnnoise/celt_lpc.c \
	rnnoise/denoise.c \
	rnnoise/kiss_fft.c \
	rnnoise/nnet.c \
	rnnoise/nnet_default.c \
	rnnoise/parse_lpcnet_weights.c \
	rnnoise/pitch.c \
	rnnoise/rnn.c \
	rnnoise/rnnoise_data.c \
	rnnoise/rnnoise_tables.c

librnenhance.$(LIBEXT): rn_denoise.c rn_denoise.h $(RNNOISE_SRC)
	$(CC) $(CFLAGS) $(SHARED) -o $@ rn_denoise.c $(RNNOISE_SRC) -lm

.PHONY: clean
clean:
	rm -f librnenhance.dylib librnenhance.so
