#include "ap_fixed.h"
#include <cmath>
#include <cstdio>
#include <iostream>

using input_t  = ap_fixed<28, 10, AP_RND, AP_SAT>;
using weight_t = ap_fixed<22, 1, AP_RND, AP_SAT>;
using accum_t  = ap_fixed<40, 13, AP_RND, AP_SAT>;

void conv1d_layer1(const input_t input[50][22],
                   const weight_t weights[32][22][3],
                   const accum_t bias[32],
                   accum_t output[32][48]);

static bool read_exact(FILE *f, float *dst, size_t n, const char *name) {
    const size_t got = std::fread(dst, sizeof(float), n, f);
    if (got != n) {
        std::cerr << "Short read for " << name << ": " << got << "/" << n << " floats\n";
        return false;
    }
    return true;
}

int main() {
    FILE *f = std::fopen("conv1d_layer1_test.bin", "rb");
    if (!f) {
        std::cerr << "Missing conv1d_layer1_test.bin in the C-simulation working directory\n";
        return 2;
    }
    static float x_f[50][22], w_f[32][22][3], b_f[32], golden[32][48];
    static input_t x[50][22];
    static weight_t w[32][22][3];
    static accum_t b[32], y[32][48];
    bool ok = read_exact(f, &x_f[0][0], 50 * 22, "input") &&
              read_exact(f, &w_f[0][0][0], 32 * 22 * 3, "weights") &&
              read_exact(f, b_f, 32, "bias") &&
              read_exact(f, &golden[0][0], 32 * 48, "golden output");
    const int trailing = std::fgetc(f);
    std::fclose(f);
    if (!ok || trailing != EOF) return 2;

    for (int t = 0; t < 50; ++t)
        for (int ic = 0; ic < 22; ++ic) x[t][ic] = x_f[t][ic];
    for (int oc = 0; oc < 32; ++oc) {
        b[oc] = b_f[oc];
        for (int ic = 0; ic < 22; ++ic)
            for (int k = 0; k < 3; ++k) w[oc][ic][k] = w_f[oc][ic][k];
    }

    conv1d_layer1(x, w, b, y);
    float max_abs = 0.0f;
    int failures = 0;
    for (int oc = 0; oc < 32; ++oc) {
        for (int t = 0; t < 48; ++t) {
            const float actual = static_cast<float>(y[oc][t]);
            const float err = std::fabs(actual - golden[oc][t]);
            if (err > max_abs) max_abs = err;
            const float limit = 1.0e-3f + 1.0e-4f * std::fabs(golden[oc][t]);
            if (!std::isfinite(actual) || err > limit) {
                if (failures < 10)
                    std::cerr << "Mismatch [" << oc << "][" << t << "]: got " << actual
                              << ", expected " << golden[oc][t] << ", error " << err
                              << " > " << limit << "\n";
                ++failures;
            }
        }
    }
    if (failures) {
        std::cerr << "FAIL: " << failures << "/1536 outputs; max abs error=" << max_abs << "\n";
        return 1;
    }
    std::cout << "PASS: 1536 outputs match; max abs error=" << max_abs << "\n";
    return 0;
}
