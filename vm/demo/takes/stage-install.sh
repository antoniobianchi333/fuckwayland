#!/bin/sh
# stage the install take: nothing but this terminal on screen, and the pointer
# parked out of the way.  The tools are not installed yet, which is the point of
# the take, so the pointer has to be parked before the package is purged.
pkill -f gnome-text-editor
pkill -x warandr
pkill -x nautilus
sleep 0.5
