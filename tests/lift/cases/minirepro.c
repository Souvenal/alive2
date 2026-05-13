// Reproduce: BL to defined function + strb to adrp address
char buf[16];
__attribute__((noinline))
void helper(int n) {
    buf[0] = '0' + n;
}
int main(int argc, char **argv) {
    buf[0] = 'X';
    helper(5);
    buf[0] = 'Y';
    return 0;
}
