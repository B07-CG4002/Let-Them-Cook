#include <iostream>
#include <ap_int.h>

void multiply_accumulate(
    const ap_int<16> a[16],
    const ap_int<16> b[16],
    ap_int<32>& result
);

int main() {
    ap_int<16> a[16];
    ap_int<16> b[16];
    ap_int<32> result = 0;

    ap_int<32> expected = 0;

    for (int i = 0; i < 16; i++) {
        a[i] = i + 1;
        b[i] = 2;
        expected += a[i] * b[i];
    }

    multiply_accumulate(a, b, result);

    if (result != expected) {
        std::cout << "TEST FAILED\n";
        std::cout << "Expected: " << expected << "\n";
        std::cout << "Actual: " << result << "\n";
        return 1;
    }

    std::cout << "TEST PASSED\n";
    std::cout << "Result: " << result << "\n";

    return 0;
}