// r8brain-free-src を C ABI で包む薄いラッパー(ctypes 用)
// "Sample rate converter designed by Aleksey Vaneev of Voxengo"
#include "CDSPResampler.h"
#include <cstring>
#include <cmath>
#include <vector>

#ifdef _WIN32
#define R8BS_API extern "C" __declspec(dllexport)
#else
#define R8BS_API extern "C" __attribute__((visibility("default")))
#endif

// 1チャンネル分を丸ごと変換(RAM事前変換向け)。戻り値=出力サンプル数、失敗<0
// phase: 0=linear, 1=minimum
R8BS_API int r8bs_convert(double src, double dst, double trans_band, double atten,
                          int phase, const double* in, long n_in,
                          double* out, long out_cap)
{
    try {
        const int CHUNK = 65536;
        r8b::CDSPResampler rs(src, dst, CHUNK, trans_band, atten,
            phase == 1 ? r8b::fprMinPhase : r8b::fprLinearPhase);
        const long want = (long)std::llround((double)n_in * dst / src);
        if (want > out_cap) return -2;
        long produced = 0, pos = 0;
        std::vector<double> buf(CHUNK);
        while (produced < want) {
            int l = CHUNK;
            if (pos < n_in) { l = (int)std::min<long>(CHUNK, n_in - pos);
                              std::memcpy(buf.data(), in + pos, l * sizeof(double)); pos += l; }
            else { std::memset(buf.data(), 0, CHUNK * sizeof(double)); } // flush
            double* op = nullptr;
            int ol = rs.process(buf.data(), l, op);
            if (ol > 0) {
                long take = std::min<long>(ol, want - produced);
                std::memcpy(out + produced, op, take * sizeof(double));
                produced += take;
            }
            if (pos >= n_in && l == CHUNK && ol == 0 && produced == 0) break;
        }
        return (int)produced;
    } catch (...) { return -1; }
}

// ストリーミング用
R8BS_API void* r8bs_new(double src, double dst, int max_in, double tb, double atten, int phase)
{
    try { return new r8b::CDSPResampler(src, dst, max_in, tb, atten,
            phase == 1 ? r8b::fprMinPhase : r8b::fprLinearPhase); }
    catch (...) { return nullptr; }
}
R8BS_API int r8bs_process(void* h, const double* in, int n, double* out, int out_cap)
{
    auto* rs = (r8b::CDSPResampler*)h;
    std::vector<double> tmp(in, in + n); // process()は入力を破壊し得るためコピー
    double* op = nullptr;
    int ol = rs->process(tmp.data(), n, op);
    if (ol > out_cap) return -2;
    if (ol > 0) std::memcpy(out, op, ol * sizeof(double));
    return ol;
}
R8BS_API int r8bs_max_out(void* h, int n) { return ((r8b::CDSPResampler*)h)->getMaxOutLen(n); }
R8BS_API void r8bs_clear(void* h) { ((r8b::CDSPResampler*)h)->clear(); }
R8BS_API void r8bs_free(void* h) { delete (r8b::CDSPResampler*)h; }
R8BS_API const char* r8bs_version() { return R8B_VERSION; }
