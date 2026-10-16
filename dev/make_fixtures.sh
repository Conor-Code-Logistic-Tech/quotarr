#!/bin/sh
# Generate small test videos + subtitles for dev/mock_plex.py. Needs ffmpeg with libx264/libx265.
set -e
OUT=${1:-/fixtures}
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
mkdir -p "$OUT/TV/Test Show/Season 01" "$OUT/TV/Another Show/Season 01" "$OUT/subs"

cat > "$OUT/subs/main.srt" <<'EOF'
1
00:00:01,000 --> 00:00:03,500
<i>Good news, everyone!</i>

2
00:00:04,000 --> 00:00:06,000
FRY: I'm gonna build my own theme park

3
00:00:06,100 --> 00:00:08,500
with blackjack and hookers.

4
00:00:09,000 --> 00:00:11,000
[LAUGHING]

5
00:00:11,500 --> 00:00:14,000
- Shut up and take my money!
- Bite my shiny metal ass.

6
00:00:15,000 --> 00:00:18,000
I don't want to live on this planet anymore.

7
00:00:19,000 --> 00:00:22,000
Why not Zoidberg?

8
00:00:23,000 --> 00:00:27,000
Thought you'd never ask.
EOF

cat > "$OUT/subs/spanish.srt" <<'EOF'
1
00:00:01,000 --> 00:00:03,500
¡Buenas noticias a todos!
EOF

cat > "$OUT/subs/main.ass" <<'EOF'
[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:02.00,0:00:05.00,Default,,0,0,0,,{\an8}Look at me,\NI'm the captain now.
Dialogue: 0,0:00:06.00,0:00:09.00,Default,,0,0,0,,I am the one who knocks.
Dialogue: 0,0:00:10.00,0:00:13.00,Default,,0,0,0,,{\i1}Thought you'd never ask.{\i0}
EOF

video() {  # name size extra-vf codec-args...
    name=$1; size=$2; label=$3; shift 3
    ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i "testsrc2=size=$size:rate=24:duration=30" \
        -f lavfi -i "sine=frequency=330:duration=30" \
        -vf "drawtext=fontfile=$FONT:text='$label %{pts\:hms}':fontsize=40:fontcolor=white:box=1:boxcolor=black@0.6:x=30:y=30" \
        "$@" -c:a aac -shortest "$name"
}

video "/tmp/e01.mkv" 1280x720 "S01E01" -c:v libx264 -preset ultrafast
cp /tmp/e01.mkv "$OUT/TV/Test Show/Season 01/Test Show - S01E01.mkv"

video "/tmp/e02.mkv" 1280x720 "S01E02" -c:v libx264 -preset ultrafast
ffmpeg -hide_banner -loglevel error -y -i /tmp/e02.mkv -i "$OUT/subs/main.srt" \
    -map 0:v -map 0:a -map 1:s -c copy -c:s srt -metadata:s:s:0 language=eng \
    "$OUT/TV/Test Show/Season 01/Test Show - S01E02.mkv"

video "/tmp/e03.mkv" 1280x720 "S01E03" -c:v libx264 -preset ultrafast
ffmpeg -hide_banner -loglevel error -y -i /tmp/e03.mkv -i "$OUT/subs/main.ass" \
    -map 0:v -map 0:a -map 1:s -c copy -c:s ass -metadata:s:s:0 language=eng \
    "$OUT/TV/Test Show/Season 01/Test Show - S01E03.mkv"

cp /tmp/e01.mkv "$OUT/TV/Test Show/Season 01/Test Show - S01E04.mkv"

# Anamorphic 720x480 DVD-style frame (SAR 8:9 -> 4:3 display) with embedded subs.
video "/tmp/a01.mkv" 720x480 "DVD 4x3" -c:v libx264 -preset ultrafast -aspect 4:3
ffmpeg -hide_banner -loglevel error -y -i /tmp/a01.mkv -i "$OUT/subs/main.srt" \
    -map 0:v -map 0:a -map 1:s -c copy -c:s srt -metadata:s:s:0 language=eng \
    "$OUT/TV/Another Show/Season 01/Another Show - S01E01.mkv"

# 10-bit HEVC tagged as HDR10 (PQ / BT.2020) to exercise tone mapping.
video "/tmp/a02.mkv" 1920x1080 "HDR10" -c:v libx265 -preset ultrafast -pix_fmt yuv420p10le \
    -x265-params "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc"
cp /tmp/a02.mkv "$OUT/TV/Another Show/Season 01/Another Show - S01E02.mkv"

rm -f /tmp/e0?.mkv /tmp/a0?.mkv
echo "Fixtures written to $OUT"
