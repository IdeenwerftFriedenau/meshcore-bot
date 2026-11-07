-- WirePlumber 0.4: dedicated headless dashboard user owns Bluetooth audio.
-- Install only for the dashboard user, not system-wide.
bluez_monitor.properties["with-logind"] = false

-- WirePlumber 0.4 treats a remote phone's HFP gateway as application streams.
-- Expose the two SCO directions as explicitly targetable devices instead.
-- Otherwise policy connects phone playback to the Pi's default sink and feeds
-- that sink's monitor back to the phone, bypassing the browser audio bridge.
table.insert(bluez_monitor.rules, {
  matches = {{
    { "api.bluez5.profile", "equals", "headset-audio-gateway" },
    { "factory.name", "equals", "api.bluez5.sco.source" },
  }},
  apply_properties = {
    ["media.class"] = "Audio/Source",
    ["node.autoconnect"] = false,
    ["priority.session"] = 0,
  },
})
table.insert(bluez_monitor.rules, {
  matches = {{
    { "api.bluez5.profile", "equals", "headset-audio-gateway" },
    { "factory.name", "equals", "api.bluez5.sco.sink" },
  }},
  apply_properties = {
    ["media.class"] = "Audio/Sink",
    ["node.autoconnect"] = false,
    ["priority.session"] = 0,
  },
})
