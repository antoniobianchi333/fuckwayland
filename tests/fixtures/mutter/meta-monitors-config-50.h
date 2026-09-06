/* Excerpt: mutter 50.1 src/backends/meta-monitor-config-manager.h (GPL-2.0-or-later,
   Copyright (C) 2016 Red Hat), from the 26.04 source package (header sha256
   702270a666c3c6f428ea118284c9d825bdde6234ef8351ac483bb843ccef5c2b).  One GList more
   than 46 has, which is the whole of why there is a description per generation.
   Used by tests/test_gnome_overlap.py against gnome/overlap-typelib/gen-gir.py. */
struct _MetaMonitorsConfig
{
  GObject parent;

  MetaMonitorsConfig *parent_config;
  MetaMonitorsConfigKey *key;
  GList *logical_monitor_configs;

  GList *disabled_monitor_specs;
  GList *for_lease_monitor_specs;

  MetaMonitorsConfigFlag flags;

  MetaLogicalMonitorLayoutMode layout_mode;

  MetaMonitorSwitchConfigType switch_config;
};
