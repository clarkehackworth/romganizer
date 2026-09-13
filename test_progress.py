"""ponytail: one check for the progress math, the only non-obvious logic added.
Run: python test_progress.py"""
from romganizer import _hms, _rate, _size

assert _hms(0) == "00:00"
assert _hms(59) == "00:59"
assert _hms(90) == "01:30"
assert _hms(3661) == "1:01:01"
assert _hms(None) == "--:--"          # unknown total -> no ETA
assert _hms(-1) == "--:--"            # clock went backwards
assert _hms(10**9) == "--:--"         # absurd ETA is noise, not information
assert _hms(float("nan")) == "--:--"

assert _size(0) == "0.0 B"
assert _size(2048) == "2.0 KB"
assert _size(5 * 1024**2) == "5.0 MB"
assert _size(3 * 1024**5).endswith("TB")   # clamps at TB, never falls off the end

assert _rate(512, "B") == "512.0 B/s"
assert _rate(2048, "B") == "2.0 KB/s"
assert _rate(5 * 1024**2, "B") == "5.0 MB/s"
assert _rate(3 * 1024**4, "B") == "3.0 TB/s"     # shares _size's ceiling
assert _rate(12.5, "") == "12.5/s"

print("ok")
