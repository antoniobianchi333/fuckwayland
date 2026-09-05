#!/bin/sh
# the drag: grab Virtual-2 in warandr's canvas and drop it under Virtual-1
wdotool search --name "Screen Layout Editor" windowmove %@ 313 98 windowsize %@ 720 557
sleep 0.5
wdotool mousemove 760 470 sleep 0.45 mousemove 565 262 sleep 0.55 \
  mousedown 1 sleep 0.5 \
  mousemove 559 272 sleep 0.09 mousemove 550 285 sleep 0.09 \
  mousemove 538 300 sleep 0.09 mousemove 523 315 sleep 0.09 \
  mousemove 505 329 sleep 0.09 mousemove 484 340 sleep 0.09 \
  mousemove 461 348 sleep 0.09 mousemove 437 353 sleep 0.09 \
  mousemove 417 350 sleep 0.09 mousemove 405 349 sleep 0.7 \
  mouseup 1
