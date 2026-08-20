#include <stdlib.h>
#include <string.h>

int main(void) {
    char *buf = malloc(8);
    memset(buf, 'A', 64);
    free(buf);
    return 0;
}