#!/bin/sh
# airplay-bridge: mux UxPlay's forwarded RTP into one MPEG-TS stream for Kodi.
#
#   UxPlay -vrtp  -> udp 127.0.0.1:5000  H.264 (RTP)
#   UxPlay -artp  -> udp 127.0.0.1:5002  L16 PCM 44.1 kHz stereo (RTP)
#   this bridge   -> udp 127.0.0.1:5010  MPEG-TS (H.264 + AAC) -> Kodi
#
# Kodi decodes the video in hardware (V4L2 mem2mem), which copes with the
# iPhone's odd mirror sizes and rotation; udp:// gets Kodi's realtime handling.
#  - h264parse config-interval=-1: SPS/PPS before every keyframe
#  - jitterbuffers drop late packets instead of adding delay
#  - mux latency: keep video flowing while no audio packets arrive
exec gst-launch-1.0 -e \
  udpsrc address=127.0.0.1 port=5000 buffer-size=1048576 \
    caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96" \
  ! rtpjitterbuffer latency=40 drop-on-latency=true \
  ! rtph264depay ! h264parse config-interval=-1 \
  ! video/x-h264,stream-format=byte-stream,alignment=au \
  ! queue max-size-time=200000000 leaky=downstream ! mux. \
  udpsrc address=127.0.0.1 port=5002 \
    caps="application/x-rtp,media=audio,payload=96,clock-rate=44100,encoding-name=L16,format=S16BE,channels=2" \
  ! rtpjitterbuffer latency=40 drop-on-latency=true \
  ! rtpL16depay ! audioconvert ! audioresample \
  ! avenc_aac bitrate=160000 ! aacparse \
  ! queue max-size-time=200000000 leaky=downstream ! mux. \
  mpegtsmux name=mux alignment=7 latency=60000000 \
  ! udpsink host=127.0.0.1 port=5010 sync=false
