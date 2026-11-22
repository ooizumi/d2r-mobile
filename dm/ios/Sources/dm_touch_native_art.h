#pragma once
#include "dm_touch_game_state.h"
enum { DM_TA_UNKNOWN=0, DM_TA_EMPTY=1, DM_TA_GLYPH=2, DM_TA_ATLASES=9 };
typedef struct { const char *asset; int width,height,frame_width,pitch,count; } dm_ta_atlas;
typedef struct { int x,y,width,height; } dm_ta_frame;
typedef struct { int status,atlas,normal_frame,pressed_frame,slot,effective_slot; uint32_t raw_skill_id; const char *reason; } dm_ta_glyph;
/* Presentation-only cache. Input resolution keeps its own 250 ms TTL; this state may reuse the last verified
   button image while the game thread is stalled, but only for the same scene, world, bank and binding identity. */
typedef struct {
    int valid, selected_bank, identity_scope;
    uint64_t scene_epoch, world_epoch, captured_ns, binding_identity, caller_identity, hud_identity, sample_token;
    dm_tg_mapping mapping[DM_TG_FACETS];
    uint32_t raw_skill_id[DM_TG_SLOTS];
    dm_ta_glyph glyph[2][5];
    unsigned char present[2][5];
} dm_ta_visual_cache;
extern const dm_ta_atlas dm_ta_atlases[DM_TA_ATLASES];
extern const char dm_ta_manifest_sha[],dm_ta_geometry_sha[],dm_ta_binding_sha[];
dm_ta_glyph dm_ta_skill(uint32_t raw_skill_id);
dm_ta_glyph dm_ta_current(const dm_tg_snapshot *s,uint64_t sdk_button,uint64_t host_now_ns);
/* 同上，skill_set=1 取第二套（alt 槽，原版按住 ZL） | Same as above; skill_set=1 takes the second set (alt slots, hold ZL in the original game) */
dm_ta_glyph dm_ta_current_set(const dm_tg_snapshot *s,uint64_t sdk_button,uint64_t host_now_ns,int skill_set);
/* Visual-only replay for a stalled game. sample_token is the cumulative controller-sample count: if the game
   advances, a stale binding is not reused. This must never be used to decide whether an input targets an empty slot. */
dm_ta_glyph dm_ta_visual_current_set(dm_ta_visual_cache *cache,const dm_tg_snapshot *s,uint64_t sdk_button,
                                     uint64_t host_now_ns,uint64_t sample_token,uint64_t scene_epoch,
                                     uint64_t world_epoch,int skill_set);
void dm_ta_visual_reset(dm_ta_visual_cache *cache);
int dm_ta_frame_at(int atlas,int frame,dm_ta_frame *out);
