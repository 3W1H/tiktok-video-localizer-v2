#!/usr/bin/env python3
"""Segmented parallel wrapper around render_overlays.py.

Why: render_overlays encodes one long clip single-process. On this box that is
~0.74 s/frame (measured 2026-10-07, 1080x1920, 2-3 inpaint boxes) -> ~39 min for
a 105s clip. Rendering N segments in parallel finishes in ~1/N of that, as long
as we keep the renderer fan-out low and cap x264 threads (SKILL 13.4.3: the
2026-09-27 load-230 spike came from 12 parallel segments).

Usage:
  python render_segments.py --video SRC --work-dir WORK --output OUT.mp4 \
      --profile nba --segments 4 --x264-threads 2
"""
import argparse, json, math, os, shutil, subprocess, sys, time

SK = "/Users/jason/Public/skills/tiktok-video-localizer-v2/scripts"
RENDER = os.path.join(SK, "render_overlays.py")
PY = sys.executable  # interpreter running this wrapper (portable)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--profile", default="nba")
    ap.add_argument("--segments", type=int, default=4)
    ap.add_argument("--x264-threads", type=int, default=2)
    ap.add_argument("--tmp-dir", default="/tmp/render_segments")
    args = ap.parse_args()

    probe = json.load(open(os.path.join(args.work_dir, "probe.json"), encoding="utf-8"))
    dur, fps = float(probe["duration_sec"]), float(probe["fps"])
    total = int(round(dur * fps))
    seg = int(math.ceil(total / args.segments))
    nseg = int(math.ceil(total / seg))

    tmp = args.tmp_dir
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    print(f"[seg] {total} frames / {nseg} segments x {seg} frames ({dur:.1f}s @ {fps:g}fps)",
          flush=True)

    env = dict(os.environ)

    def render(i):
        start_s = i * seg / fps
        n = min(seg, total - i * seg)
        out = os.path.join(tmp, f"seg_{i:02d}.mp4")
        cmd = [PY, RENDER, "--video", args.video, "--work-dir", args.work_dir,
               "--output", out, "--profile", args.profile,
               "--start-time", f"{start_s:.3f}", "--max-frames", str(n),
               "--x264-threads", str(args.x264_threads)]
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            print(f"[seg {i}] FAILED rc={r.returncode}\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}",
                  flush=True)
            return (i, False, time.time() - t0)
        print(f"[seg {i}] ok  start={start_s:.1f}s n={n} in {time.time()-t0:.0f}s", flush=True)
        return (i, True, time.time() - t0)

    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=nseg) as ex:
        results = list(ex.map(render, range(nseg)))
    if not all(ok for _, ok, _ in results):
        print("[seg] abort: some segments failed", flush=True)
        sys.exit(1)

    # concat (stream copy; all segments share encoder params by construction)
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for i in range(nseg):
            f.write(f"file '{os.path.join(tmp, f'seg_{i:02d}.mp4')}'\n")
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", args.output],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[seg] concat FAILED\n{r.stderr[-1500:]}", flush=True)
        sys.exit(1)
    print(f"[seg] DONE {args.output} in {time.time()-t0:.0f}s "
          f"(segments only, wall)", flush=True)


if __name__ == "__main__":
    main()
