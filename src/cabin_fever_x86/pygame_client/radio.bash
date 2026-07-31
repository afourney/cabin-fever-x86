#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./radio.sh input.wav output.wav
#
# Optional environment variables:
#
#   NOISE=0.035
#   BURSTS=8
#   BURST_LEVEL=0.38
#   SQUELCH_LEVEL=0.18
#   CLICK_LEVEL=0.65
#   SEED=12345
#
# Example:
#
#   NOISE=0.05 \
#   BURSTS=12 \
#   BURST_LEVEL=0.50 \
#   SQUELCH_LEVEL=0.22 \
#   CLICK_LEVEL=0.75 \
#   SEED=42 \
#   ./radio.sh input.wav output.wav

INPUT="${1:?Usage: $0 INPUT OUTPUT}"
OUTPUT="${2:?Usage: $0 INPUT OUTPUT}"

NOISE="${NOISE:-0.035}"
BURSTS="${BURSTS:-0}"
BURST_LEVEL="${BURST_LEVEL:-0.38}"
SQUELCH_LEVEL="${SQUELCH_LEVEL:-0.18}"
CLICK_LEVEL="${CLICK_LEVEL:-0.65}"
SEED="${SEED:-12345}"

SQUELCH_DURATION="0.150"
CLICK_DURATION="0.006"
FINAL_SILENCE="0.300"
OUTPUT_PADDING="0.750"

CLICK_OFFSET_MS="$(
    awk -v d="$SQUELCH_DURATION" '
        BEGIN { printf "%d", d * 1000 - 6 }
    '
)"

command -v ffmpeg >/dev/null 2>&1 || {
    echo "Error: ffmpeg is not installed." >&2
    exit 1
}

command -v ffprobe >/dev/null 2>&1 || {
    echo "Error: ffprobe is not installed." >&2
    exit 1
}

if [[ ! -f "$INPUT" ]]; then
    echo "Error: input file not found: $INPUT" >&2
    exit 1
fi

if ! [[ "$BURSTS" =~ ^[0-9]+$ ]]; then
    echo "Error: BURSTS must be a nonnegative integer." >&2
    exit 1
fi

DURATION="$(
    ffprobe \
        -v error \
        -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 \
        "$INPUT"
)"

if ! [[ "$DURATION" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "Error: could not determine input duration." >&2
    exit 1
fi

TAIL_MS="$(
    awk -v audio_dur="$DURATION" '
        BEGIN {
            printf "%d", audio_dur * 1000
        }
    '
)"

TOTAL_DURATION="$(
    awk \
        -v audio_dur="$DURATION" \
        -v padding="$OUTPUT_PADDING" '
        BEGIN {
            printf "%.3f", audio_dur + padding
        }
    '
)"

# Noise outputs:
#
#   1 continuous background hiss
#   BURSTS random interference bursts
#   1 constant-volume squelch tail
#   1 closing click

SPLIT_COUNT=$((BURSTS + 3))

# Mixed streams:
#
#   voice
#   background hiss
#   BURSTS interference bursts
#   squelch tail
#   closing click

MIX_COUNT=$((BURSTS + 4))

FILTER="
[0:a]
aformat=sample_rates=48000:channel_layouts=mono,
highpass=f=350:p=2,
lowpass=f=2850:p=2,
equalizer=f=1450:t=q:w=1.0:g=6,
acompressor=threshold=0.06:ratio=10:attack=2:release=60:makeup=5,
tremolo=f=4.3:d=0.18,
asoftclip=type=tanh:threshold=0.58,
volume=0.90
[voice];

[1:a]
aformat=sample_rates=48000:channel_layouts=mono,
asplit=${SPLIT_COUNT}
"

FILTER+="[hiss]"

for ((i = 0; i < BURSTS; i++)); do
    FILTER+="[burst${i}]"
done

FILTER+="[tail_rf][tail_click];

[hiss]
atrim=duration=${DURATION},
highpass=f=900:p=2,
lowpass=f=7200:p=2,
volume=${NOISE}
[hiss_out];
"

MIX_INPUTS="[voice][hiss_out]"

for ((i = 0; i < BURSTS; i++)); do
    VALUES="$(
        awk \
            -v rng_seed="$SEED" \
            -v burst_num="$i" \
            -v audio_dur="$DURATION" '
            BEGIN {
                srand(rng_seed + burst_num * 7919)

                latest_start = audio_dur - 0.45

                if (latest_start < 0.20) {
                    latest_start = 0.20
                }

                burst_start = 0.20 \
                    + rand() * (latest_start - 0.20)

                burst_dur = 0.035 \
                    + rand() * 0.145

                burst_delay_ms = int(burst_start * 1000)

                printf "%.3f %.3f %d",
                    burst_start,
                    burst_dur,
                    burst_delay_ms
            }
        '
    )"

    read -r BURST_START BURST_DUR BURST_DELAY_MS <<< "$VALUES"

    FADE_START="$(
        awk -v event_dur="$BURST_DUR" '
            BEGIN {
                printf "%.3f", event_dur * 0.55
            }
        '
    )"

    FADE_DURATION="$(
        awk -v event_dur="$BURST_DUR" '
            BEGIN {
                printf "%.3f", event_dur * 0.45
            }
        '
    )"

    FILTER+="
[burst${i}]
atrim=duration=${BURST_DUR},
highpass=f=450:p=2,
lowpass=f=6500:p=2,
asoftclip=type=hard:threshold=0.22,
volume=${BURST_LEVEL},
afade=t=in:st=0:d=0.003,
afade=t=out:st=${FADE_START}:d=${FADE_DURATION},
adelay=delays=${BURST_DELAY_MS}:all=1
[burst${i}_out];
"

    MIX_INPUTS+="[burst${i}_out]"
done

FILTER+="
[tail_rf]
atrim=duration=${SQUELCH_DURATION},
highpass=f=500:p=2,
lowpass=f=4500:p=2,
volume=${SQUELCH_LEVEL},
adelay=delays=${TAIL_MS}:all=1
[tail_rf_out];

[tail_click]
atrim=duration=${CLICK_DURATION},
highpass=f=1800:p=2,
lowpass=f=9000:p=2,
volume=${CLICK_LEVEL},
afade=t=out:st=0:d=${CLICK_DURATION},
adelay=delays=$((TAIL_MS + CLICK_OFFSET_MS)):all=1
[tail_click_out];

${MIX_INPUTS}[tail_rf_out][tail_click_out]
amix=inputs=${MIX_COUNT}:duration=longest:dropout_transition=0:normalize=0,
alimiter=limit=0.92:attack=1:release=40,
apad=pad_dur=${FINAL_SILENCE},
atrim=duration=${TOTAL_DURATION}
[out]
"

ffmpeg \
    -hide_banner \
    -loglevel error \
    -y \
    -i "$INPUT" \
    -f lavfi \
    -i "anoisesrc=color=white:amplitude=0.8:r=48000:d=${TOTAL_DURATION}:seed=${SEED}" \
    -filter_complex "$FILTER" \
    -map "[out]" \
    -c:a pcm_s16le \
    "$OUTPUT"

echo "Wrote: $OUTPUT"
