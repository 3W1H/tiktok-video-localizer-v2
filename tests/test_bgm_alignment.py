#!/usr/bin/env python3
"""BGM 对齐 / 回卷的规格断言。

为什么要写这个：2026-10-16 雷阿伦 xWT128 那条片子出厂后被用户否掉 ——
「怎么老是一个调呢，而且也不连贯」。查下来是**两个**叠加的 bug：

  1. `analyze_bgm.plan_alignment` 的 `loop_shift` 分支把偏移量的符号写反了
     （`need = -offset`，那是互补值 曲长-p，不是 p）。曲长 155.85s、drop 1.88s、
     高潮 146.0s 时给出 144.12s，正确值应是 11.73s。
  2. `audio_processor.build_bgm_track` 用 `atrim=start=<offset>` 之后再 `aloop`，
     于是循环体是**切剩下的那一小段**（155.85-144.12 = 11.73s），不是整曲。
     两者叠加 → 同一 11.73s 片段重复 13.4 次，每 11.75s 出现一个 5.00s 静音
     空洞（Five Armies 曲尾是淡出到数字静音），全片 41% 是静音。

几何关系（必须成立的不变式）：设变速 s 之后曲长 L、曲内 drop 位于 d，
从曲内第 p 秒起播、播到曲尾后回卷到曲首，则 drop 出现在
    ``p + T ≡ d  (mod L)``
即 ``p = (d - T) mod L``。本文件把这句写成断言。

Run either way:

    /Users/jason/opt/miniconda3/bin/python tests/test_bgm_alignment.py
    python -m pytest tests/test_bgm_alignment.py -q
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import analyze_bgm as A  # noqa: E402


def first_drop_landing(p, drop_t, play_len):
    """从曲内第 p 秒起播、回卷播放时，drop 第一次出现在第几秒（视频时间轴）。

    注意：曲子比视频短时 drop 会**每循环一次再出现一次**，所以判定落点要比
    `T ≡ 首次落点 (mod 曲长)`，不能只比第一次。
    """
    if drop_t >= p:
        return drop_t - p
    return (play_len - p) + drop_t


def where_drop_lands(p, drop_t, play_len, target):
    """drop 落在离 target 最近的那一次循环里的时刻。"""
    first = first_drop_landing(p, drop_t, play_len)
    return first + round((target - first) / play_len) * play_len


# --------------------------------------------------------------------------
def test_drop_is_aligned_whenever_it_can_be_done_without_a_seam():
    """**能零拼接就零拼接**：解出来的偏移必须是「纯偏移」那一种。

    2026-10-16 新增的全局规则（`plan_alignment` 的零拼接优先分支）：
    只要片子装得进曲子的有用段，就只剩两种结局 ——

      * `offset`  —— `offset ≥ 0` 且 `offset + 片长 ≤ 有用段末端`：对齐且零拼接；
      * `as_is`   —— 对齐需要回卷（起点为负 / 塞不下）时**放弃对齐**，从曲首平铺。

    也就是说 `loop_shift` 只允许出现在「曲子真的比片子短」的例子里。
    """
    cases = [
        # (曲长, drop 位置, 高潮, atempo, 期望 method, drop 是否必须落在高潮上)
        (155.847, 1.88, 146.0, 1.0, "loop_shift", True),     # 雷阿伦原案：曲长 155.85 < 片长 157.71，非回卷不可
        (226.456, 156.62, 146.0, 0.9624, "offset", True),    # 雷阿伦定稿：起播 16.74s 就够放完
        (169.700, 110.00, 100.0, 1.0, "offset", True),       # 起播 10s + 157.71s = 167.71s < 169.70s
        (222.929, 57.62, 146.0, 0.9624, "as_is", False),     # Malicious：drop 在前，对齐要回卷 ⇒ 宁可不对齐
        (169.700, 120.00, 100.0, 1.0, "as_is", False),       # 起播 20s + 157.71s 溢出 ⇒ 同样平铺
        (60.000, 5.00, 120.0, 1.05, "loop_shift", True),     # 曲长远短于片长，回卷是唯一出路
    ]
    for L, d0, T, s, want_method, want_landing in cases:
        rhythm = {"climax_mid": T, "duration_sec": 157.711, "bpm_mid": 138.2}
        plan = A.plan_alignment(L, [{"t": d0}], None, rhythm, None, atempo=s)
        assert plan["method"] == want_method, (
            f"L={L} drop={d0} T={T} s={s}: method={plan['method']} 应为 {want_method}")
        p = plan["start_offset_sec"]
        play_len = L / s
        assert 0.0 <= p < play_len + 0.05, (
            f"偏移 {p} 必须落在 [0, 曲长) 内（曲长 {play_len:.2f}s）")
        if want_landing:
            land = where_drop_lands(p, d0 / s, play_len, T)
            assert abs(land - T) <= 0.05, (
                f"L={L} drop={d0} s={s}: 偏移 {p} 让 drop 落在 {land:.2f}s，"
                f"但高潮在 {T}s —— 差 {abs(land - T):.2f}s")
            print(f"    L={L:7.3f} drop={d0:6.2f} s={s:6.4f} → 偏移 {p:7.3f}s，"
                  f"drop 落点 {land:7.2f}s（目标 {T}）")
        else:
            assert p == 0.0, f"平铺方案的偏移必须是 0，实际 {p}"
            assert play_len >= 157.711, "平铺的前提是曲子装得下整条片子"
            print(f"    L={L:7.3f} drop={d0:6.2f} s={s:6.4f} → 平铺（偏移 0），"
                  f"曲长 {play_len:.2f}s ≥ 片长 157.71s，刻意不对齐、零接缝")


def test_loop_shift_offset_is_not_the_complement():
    """回归断言：`loop_shift` 的偏移不能是「曲长 − 正确值」那个互补值。

    旧代码 `need = -offset` 给出的正是互补值，而互补值会让循环体只剩曲尾的
    百分之几 —— 这就是 13.4 次重复的直接来源。
    """
    L, d0, T = 155.847, 1.88, 146.0
    rhythm = {"climax_mid": T, "duration_sec": 157.711, "bpm_mid": 138.2}
    p = A.plan_alignment(L, [{"t": d0}], None, rhythm, None, atempo=1.0)["start_offset_sec"]
    correct = (d0 - T) % L                       # 11.727
    complement = L - correct                     # 144.12 —— 旧代码的错误值
    assert abs(p - correct) < 0.05, f"偏移应为 {correct:.3f}s，实际 {p:.3f}s"
    assert abs(p - complement) > 1.0, (
        f"偏移 {p:.3f}s 又回到互补值 {complement:.3f}s 了 —— 循环体会只剩 "
        f"{L - p:.2f}s")
    body = L - p
    assert body > 0.5 * L, (
        f"回卷后的第一段只有 {body:.2f}s（曲长 {L:.2f}s）—— 太短，会把同一小段"
        "反复搬运")


def test_offset_is_measured_on_the_stretched_timeline():
    """偏移必须在 atempo **之后**的时间轴上算。

    引擎的滤镜顺序是 `atempo → 回卷 → 循环`，所以 `start_offset_sec` 解释在
    变速后的音频上；拿原始时间轴的 drop 位置去算，误差 = d·(1/s − 1)。
    Nightmare Machine 那例 s=0.9624、d=156.62s，漏掉换算会差 6.1s ≈ 14 拍。
    """
    L, d0, T, s = 226.456, 156.62, 146.0, 0.9624
    rhythm = {"climax_mid": T, "duration_sec": 157.711, "bpm_mid": 138.2}
    p = A.plan_alignment(L, [{"t": d0}], None, rhythm, None, atempo=s)["start_offset_sec"]
    naive = d0 - T                                  # 没除 s 的错误值
    assert abs(p - naive) > 1.0, (
        f"偏移 {p:.3f}s 与「不做变速换算」的 {naive:.3f}s 太接近 —— 少算了 /s")
    land = where_drop_lands(p, d0 / s, L / s, T)
    assert abs(land - T) <= 0.10, f"drop 落在 {land:.2f}s，目标 {T}s"


def test_no_wrap_when_the_track_already_fits():
    """曲子够长且 drop 靠后时，必须**不回卷**一次放完 —— 零拼接。

    这是雷阿伦定稿选 Nightmare Machine 的理由：起播 16.74s + 157.71s = 174.5s
    < 变速后曲长 235.3s，全程不产生任何接缝。
    """
    L, d0, T, s = 226.456, 156.62, 146.0, 0.9624
    rhythm = {"climax_mid": T, "duration_sec": 157.711, "bpm_mid": 138.2}
    plan = A.plan_alignment(L, [{"t": d0}], None, rhythm, None, atempo=s)
    assert plan["method"] == "offset", (
        f"method={plan['method']} —— drop 在变速后 {d0 / s:.1f}s > 高潮 {T}s，"
        "应当走 offset 分支（不回卷）")
    p = plan["start_offset_sec"]
    assert p + 157.711 <= L / s, (
        f"起播 {p:.2f}s + 片长 157.71s 超出变速后曲长 {L / s:.2f}s，仍会回卷")
    print(f"    起播 {p:.2f}s，播到 {p + 157.711:.2f}s，曲长 {L / s:.2f}s → 无接缝")


def test_flat_video_never_wraps_into_the_tail():
    """视频强度曲线为 `flat` 时，只要片子装得下，就必须从曲首平铺、不许回卷。

    2026-10-16 实测 08 罗斯 / Ready Aim Fire：曲长 216.95s、曲尾淡出从 211.75s 起、
    最后 2.0s 是数字静音；片子 202.66s、profile=flat。旧的 drop 对齐逻辑给出
    offset=54s 并回卷，把曲尾那 3.5s 静音搬到了**视频 159–163s**——片子中间凭空
    空了 3.5 秒。正确做法是从 0 平铺：202.66s < 211.75s，既铺得满又碰不到尾巴。
    """
    L, video, useful_end = 216.947, 202.664, 211.75
    rhythm = {"climax_mid": 56.12, "duration_sec": video,
              "bpm_mid": 104.8, "intensity_profile": "flat"}
    plan = A.plan_alignment(L, [{"t": 2.12}], None, rhythm, None, atempo=1.0,
                            bgm_useful_end=useful_end)
    assert plan["method"] == "as_is", (
        f"flat 视频给出 method={plan['method']} —— 应当 as_is（不回卷）")
    assert plan["start_offset_sec"] == 0.0, (
        f"偏移应为 0，实际 {plan['start_offset_sec']}s")
    assert plan["start_offset_sec"] + video <= useful_end, (
        "平铺后仍越过曲尾有用端，会把淡出/静音播进片子")
    print(f"    片长 {video:.2f}s < 有用端 {useful_end:.2f}s → 曲首平铺，零回卷")

    # 反例：片子装不下时，必须回到原来的 drop 对齐逻辑（该回卷就回卷）
    long_rhythm = dict(rhythm)
    long_rhythm["duration_sec"] = 260.0
    plan2 = A.plan_alignment(L, [{"t": 2.12}], None, long_rhythm, None, atempo=1.0,
                             bgm_useful_end=useful_end)
    assert plan2["method"] == "loop_shift", (
        f"片长 260s 超过曲长 {L}s，必须回卷，却给出 {plan2['method']}")
    print(f"    片长 260.00s > 曲长 {L:.2f}s → 仍回卷（method={plan2['method']}）")


def test_wave_video_also_never_wraps_when_it_fits():
    """`wave` 曲线同样适用「能平铺就平铺」—— 这是 2026-10-16 第二批返修暴露的漏洞。

    旧规则只在 `intensity_profile == "flat"` 时强制平铺，于是这三条片子继续回卷、
    继续在片子中间留静音洞：

      * 03 奥尼尔 6 场：片长 98.37s、`Harmful or Fatal` 曲长 330.74s，
        旧方案 offset=242.62s → **242.62 + 98.37 = 340.99s > 330.74s**，
        在视频 85.5–88.0s 回卷，留下 2.5s 静音洞；
      * 约基奇：片长 96.60s、`Rocket` 146.97s，offset=70.2s → 回卷于 76.77s，洞 3.25s；
      * 马布里：片长 143.96s、`Cut and Run` 214.83s，offset=140.5s → 回卷于 74.33s。

    三条**本来一次就播得完**（曲长都 ≥ 片长），回卷纯属自找。新规则的判据不是
    「profile 是不是 flat」，而是「`offset + 片长` 有没有塞进有用段」。
    """
    # 逐条按实测参数复算
    cases = [
        (330.736, 291.62, 49.00, 98.367),   # 03 奥尼尔：offset=242.62 → 242.62+98.37=340.99 溢出
        (146.965, 148.78, 78.58, 96.596),   # 约基奇：offset=70.20  → 166.80 溢出
        (214.831, 208.12, 67.62, 143.964),  # 马布里：offset=140.50 → 284.46 溢出
    ]
    for L, d0, T, video in cases:
        rhythm = {"climax_mid": T, "duration_sec": video,
                  "bpm_mid": 130.0, "intensity_profile": "wave"}
        plan = A.plan_alignment(L, [{"t": d0}], None, rhythm, None, atempo=1.0)
        assert plan["method"] == "as_is", (
            f"曲长 {L:.2f}s ≥ 片长 {video:.2f}s 却给出 method={plan['method']} —— "
            "应当从曲首平铺、一次播完")
        assert plan["start_offset_sec"] == 0.0, (
            f"平铺方案偏移必须为 0，实际 {plan['start_offset_sec']}")
        assert video <= L, "平铺的前提是曲子装得下整条片子"
        print(f"    曲长 {L:7.3f}s ≥ 片长 {video:6.3f}s → 平铺（原方案 "
              f"offset={d0 - T:7.2f}s 会溢出 {d0 - T + video - L:+.2f}s，非回卷不可）")

    # 反例：曲子装不下时，drop 对齐照旧生效
    rhythm = {"climax_mid": 146.0, "duration_sec": 200.0,
              "bpm_mid": 130.0, "intensity_profile": "wave"}
    plan = A.plan_alignment(155.847, [{"t": 1.88}], None, rhythm, None, atempo=1.0)
    assert plan["method"] == "loop_shift", (
        f"片长 200s > 曲长 155.85s，必须回卷，却给出 {plan['method']}")
    print(f"    片长 200.00s > 曲长 155.85s + profile=wave → 仍回卷"
          f"（method={plan['method']}，这是回卷唯一被允许的情形）")


def test_useful_end_ignores_the_fade_out_tail():
    """`useful_end` 必须落在曲尾淡出**开始**的位置，而不是文件结束。"""
    # 10s 音频：前 8s 电平 0 dB，最后 2s 掉到 -60 dB
    times = [round(i * 0.10, 2) for i in range(100)]
    db = [-20.0] * 80 + [-60.0] * 20
    end = A.useful_end(times, db)
    assert end is not None and 7.5 <= end <= 8.2, (
        f"有用端算成 {end}s，应≈8s（淡出起点），而不是 9.9s（文件尾）")
    print(f"    淡出起点 8.0s → useful_end={end}s")


def test_useful_start_finds_the_music_onset():
    """`useful_start` 要报出曲首数字静音垫片结束的位置，而不是文件开头。"""
    # 10s 音频：前 1s 是数字静音，之后电平 -20 dB
    times = [round(i * 0.10, 2) for i in range(100)]
    db = [-60.0] * 10 + [-20.0] * 90
    start = A.useful_start(times, db)
    assert start is not None and 0.6 <= start <= 0.95, (
        f"起音点算成 {start}s，应≈0.8s（垫片结束），而不是 0s 或 1.0s")
    print(f"    垫片 1.0s → useful_start={start}s")

    # 反例：本来就从前 0.1s 起音的曲子，不许报垫片（否则会削掉正常起拍）
    db2 = [-20.0] * 100
    assert A.useful_start(times, db2) is None, (
        "这首曲子没有垫片，useful_start 应为 None")


def test_head_pad_is_trimmed_never_wrapped():
    """曲首垫片必须用 `head_skip_sec`（截断）处理，且**不得**引入回卷。

    2026-10-16 韦德 09 实测：`Black Vortex` 前 0.83s 低于 −60 dBFS。若把垫片
    当 `start_offset_sec` 走回卷分支，那段静音会被搬到**片尾**去（凭空多一截
    片尾静音）；正确做法是保持 offset=0、只把输入起点往后挪。
    """
    L, video, useful_end, useful_start = 169.718, 126.317, 166.0, 0.62
    rhythm = {"climax_mid": 100.5, "duration_sec": video,
              "bpm_mid": 142.6, "intensity_profile": "flat"}
    plan = A.plan_alignment(L, [{"t": 0.62}], None, rhythm, None, atempo=1.0,
                            bgm_useful_end=useful_end,
                            bgm_useful_start=useful_start)
    assert plan["method"] == "as_is", f"method={plan['method']}，应为 as_is"
    assert plan["start_offset_sec"] == 0.0, (
        f"offset={plan['start_offset_sec']} —— 平铺必须是 0，任何非零值都会走"
        "回卷分支、把垫片搬到片尾")
    assert plan.get("head_skip_sec") == useful_start, (
        f"head_skip_sec={plan.get('head_skip_sec')}，应为 {useful_start}")
    assert useful_start + video <= useful_end, "跳过垫片后仍越过曲尾有用端"
    print(f"    片长 {video:.2f}s；offset=0，head_skip={useful_start}s，"
          f"播到 {useful_start + video:.2f}s < 有用端 {useful_end:.2f}s → 一次连续播放")


def test_no_head_skip_when_aligning_by_drop():
    """走 drop 对齐 / 回卷分支时不许塞 `head_skip_sec` —— 那两个分支已经有偏移，
    再截一次头会把方案算好的落点整体挪掉。"""
    rhythm = {"climax_mid": 146.0, "duration_sec": 157.711, "bpm_mid": 138.2}
    plan = A.plan_alignment(226.456, [{"t": 156.62}], None, rhythm, None,
                            atempo=0.9624, bgm_useful_start=0.62)
    assert plan["method"] == "offset"
    assert not plan.get("head_skip_sec"), (
        f"offset 分支不该有 head_skip_sec，却有 {plan.get('head_skip_sec')}")


# --------------------------------------------------------------------------
def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        print(f"\n[{name}]")
        try:
            fn()
        except AssertionError as exc:
            failed.append((name, str(exc)))
            print(f"  FAIL: {exc}")
        except Exception as exc:                              # noqa: BLE001
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  ERROR: {type(exc).__name__}: {exc}")
        else:
            print("  ok")
    print("\n" + "=" * 64)
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}")
        for name, msg in failed:
            print(f"  - {name}: {msg[:200]}")
        return 1
    print(f"PASSED {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
