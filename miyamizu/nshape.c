/* ノイズシェーピング付き再量子化(誤差フィードバック + TPDF ディザ)。
 * 入力: double (-1..1), 出力: valid_bits に量子化した left-justified int32。
 * NTF(z) = 1 - sum_k c[k] z^-(k+1)。状態(誤差履歴・乱数)はチャンネルごとに呼び出し間で引き継ぐ。 */
#include <stdint.h>
#include <math.h>

static inline uint64_t xs(uint64_t *s) {
    uint64_t x = *s;
    x ^= x >> 12; x ^= x << 25; x ^= x >> 27;
    *s = x;
    return x * 0x2545F4914F6CDD1DULL;
}

int nshape_run(const double *x, int32_t *out, long n, int ch, int bits,
               const double *c, int taps, double *hist, uint64_t *rng, int dither)
{
    const double scale = (double)((int64_t)1 << (bits - 1));
    const int sh = 32 - bits;
    int clips = 0;
    for (long i = 0; i < n; i++) {
        for (int k = 0; k < ch; k++) {
            double *h = hist + (long)k * taps;      /* h[0] が直前の誤差 */
            double v = x[i * ch + k] * scale;
            double fb = 0.0;
            for (int j = 0; j < taps; j++) fb += c[j] * h[j];
            v -= fb;
            double d = 0.0;
            if (dither) {
                double u1 = (xs(&rng[k]) >> 11) * (1.0 / 9007199254740992.0);
                double u2 = (xs(&rng[k]) >> 11) * (1.0 / 9007199254740992.0);
                d = u1 - u2;
            }
            double q = rint(v + d);
            if (q > scale - 1.0) { q = scale - 1.0; clips++; }
            if (q < -scale)      { q = -scale;      clips++; }
            double e = q - v;
            if (e > 4.0 || e < -4.0) e = 0.0;       /* クリップ等で暴れた誤差は捨てる */
            for (int j = taps - 1; j > 0; j--) h[j] = h[j - 1];
            h[0] = e;
            out[i * ch + k] = (int32_t)((int64_t)q << sh);
        }
    }
    return clips;
}
