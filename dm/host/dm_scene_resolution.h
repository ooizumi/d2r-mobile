#pragma once
#include <stdint.h>

// The UI only queues a height. Guest render-target changes run in the existing
// system-notification polling context, never on the Android UI/present thread.
int dm_scene_resolution_request(int height);
typedef struct dm_scene_resolution_state {
    int requested_height;
    uint32_t scene_width, scene_height, output_width, output_height;
} dm_scene_resolution_state;
void dm_scene_resolution_read(dm_scene_resolution_state *out);
