/* Excerpt: mutter 46.0/46.2 src/backends/meta-monitor-config-manager.h (GPL-2.0-or-later,
   Copyright (C) 2016 Red Hat).  The declaration --unsafe-gnome-overlap's type
   description for libmutter-14 is derived from; the two 24.04 tarballs' headers are
   byte-identical (sha256 5f131fc161473a2f038cf33fa32c8dc5a99976300c83201760fb39a666c2dbd4).
   Used by tests/test_gnome_overlap.py against gnome/overlap-typelib/gen-gir.py. */
struct _MetaMonitorsConfig
{
  GObject parent;

  MetaMonitorsConfig *parent_config;
  MetaMonitorsConfigKey *key;
  GList *logical_monitor_configs;

  GList *disabled_monitor_specs;

  MetaMonitorsConfigFlag flags;

  MetaLogicalMonitorLayoutMode layout_mode;

  MetaMonitorSwitchConfigType switch_config;
};

