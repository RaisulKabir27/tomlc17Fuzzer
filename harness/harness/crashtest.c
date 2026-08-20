#include <stdlib.h>

int main(void) {
    volatile char *buf = (volatile char *)malloc(8);

    for (int i = 0; i < 64; i++) {
        buf[i] = 'A';
    }

    free((void *)buf);
    return 0;
}