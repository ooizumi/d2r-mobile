#include <string.h>
static dm_ta_glyph unknown(uint32_t id,const char *reason) {
    return (dm_ta_glyph){DM_TA_UNKNOWN,-1,-1,-1,-1,-1,id,reason};
}
int dm_ta_frame_at(int atlas,int frame,dm_ta_frame *out) {
    if(!out || atlas<0 || atlas>=DM_TA_ATLASES || frame<0)return 0;
    const dm_ta_atlas *a=&dm_ta_atlases[atlas];
    if(frame>=a->count || frame*a->pitch+a->frame_width>a->width)return 0;
    *out=(dm_ta_frame){frame*a->pitch,0,a->frame_width,a->height};return 1;
}
dm_ta_glyph dm_ta_skill(uint32_t id) {
    dm_ta_glyph g=unknown(id,"unknown_skill");
    if(id==UINT32_MAX){g.status=DM_TA_EMPTY;g.reason="empty_slot";return g;}
    for(size_t i=0;i<sizeof rows/sizeof rows[0];i++)if(rows[i].id==id) {
        dm_ta_frame frame;
        if(!dm_ta_frame_at(rows[i].atlas,rows[i].frame,&frame) || !dm_ta_frame_at(rows[i].atlas,rows[i].frame+1,&frame))return g;
        g.status=DM_TA_GLYPH;g.atlas=rows[i].atlas;g.normal_frame=rows[i].frame;g.pressed_frame=rows[i].frame+1;g.reason="official_raw_id";return g;
    }
    return g;
}
static int snapshot_valid(const dm_tg_snapshot *s) {
    return s && s->supported && s->valid && s->ready && s->source==DM_TG_SOURCE_NATIVE_GETTER &&
           s->identity_scope==DM_TG_IDENTITY_NATIVE && s->captured_ns &&
           (s->selected_bank==0 || s->selected_bank==16);
}
static int snapshot_fresh(const dm_tg_snapshot *s,uint64_t now) {
    return snapshot_valid(s) && now>=s->captured_ns && now-s->captured_ns<=DM_TG_MAX_AGE_NS;
}
dm_ta_glyph dm_ta_current(const dm_tg_snapshot *s,uint64_t button,uint64_t now) { return dm_ta_current_set(s,button,now,0); }
dm_ta_glyph dm_ta_current_set(const dm_tg_snapshot *s,uint64_t button,uint64_t now,int skill_set) {
    dm_ta_glyph g=unknown(UINT32_MAX,"empty_unknown");
    if(!snapshot_fresh(s,now))return g;
    uint32_t logical=0;
    switch(button){case DM_BTN_A:logical=0x2000;break;case DM_BTN_B:logical=0x1000;break;
        case DM_BTN_X:logical=0x8000;break;case DM_BTN_Y:logical=0x4000;break;case DM_BTN_R:logical=0x200;break;default:return g;}
    int slot=-1;
    for(int i=0;i<DM_TG_FACETS;i++)if(s->mapping[i].logical==logical){if(slot>=0)return g;slot=(int)(skill_set?s->mapping[i].alt:s->mapping[i].normal);}
    if(slot<0 || slot>=(skill_set?12:6))return g;
    g=dm_ta_skill(s->raw_skill_id[slot]);g.slot=slot;g.effective_slot=s->selected_bank+slot;return g;
}
void dm_ta_visual_reset(dm_ta_visual_cache *cache) {
    if(cache)memset(cache,0,sizeof *cache);
}
static int visual_button_index(uint64_t button) {
    switch(button){case DM_BTN_A:return 0;case DM_BTN_B:return 1;case DM_BTN_X:return 2;
        case DM_BTN_Y:return 3;case DM_BTN_R:return 4;default:return -1;}
}
static int visual_same_state(const dm_ta_visual_cache *cache,const dm_tg_snapshot *s,
                             uint64_t scene_epoch,uint64_t world_epoch) {
    if(!cache->valid || !s || cache->scene_epoch!=scene_epoch || cache->world_epoch!=world_epoch ||
       cache->captured_ns!=s->captured_ns || cache->selected_bank!=s->selected_bank ||
       cache->binding_identity!=s->binding_identity || cache->caller_identity!=s->caller_identity ||
       cache->hud_identity!=s->hud_identity || cache->identity_scope!=s->identity_scope ||
       s->scene_epoch!=scene_epoch || s->world_epoch!=world_epoch)return 0;
    return !memcmp(cache->mapping,s->mapping,sizeof cache->mapping) &&
           !memcmp(cache->raw_skill_id,s->raw_skill_id,sizeof cache->raw_skill_id);
}
static void visual_remember_state(dm_ta_visual_cache *cache,const dm_tg_snapshot *s,
                                  uint64_t scene_epoch,uint64_t world_epoch,uint64_t sample_token) {
    cache->valid=1;cache->scene_epoch=scene_epoch;cache->world_epoch=world_epoch;
    cache->captured_ns=s->captured_ns;cache->selected_bank=s->selected_bank;
    cache->binding_identity=s->binding_identity;cache->caller_identity=s->caller_identity;
    cache->hud_identity=s->hud_identity;cache->identity_scope=s->identity_scope;
    cache->sample_token=sample_token;
    memcpy(cache->mapping,s->mapping,sizeof cache->mapping);
    memcpy(cache->raw_skill_id,s->raw_skill_id,sizeof cache->raw_skill_id);
}
dm_ta_glyph dm_ta_visual_current_set(dm_ta_visual_cache *cache,const dm_tg_snapshot *s,uint64_t button,
                                     uint64_t now,uint64_t sample_token,uint64_t scene_epoch,
                                     uint64_t world_epoch,int skill_set) {
    dm_ta_glyph g=unknown(UINT32_MAX,"empty_unknown");
    int index=visual_button_index(button);
    if(!cache || index<0 || (skill_set!=0 && skill_set!=1) || !snapshot_valid(s) || now<s->captured_ns ||
       !scene_epoch || !world_epoch || s->scene_epoch!=scene_epoch || s->world_epoch!=world_epoch) {
        dm_ta_visual_reset(cache);return g;
    }
    if(snapshot_fresh(s,now)) {
        if(!visual_same_state(cache,s,scene_epoch,world_epoch))dm_ta_visual_reset(cache);
        if(!cache->valid)visual_remember_state(cache,s,scene_epoch,world_epoch,sample_token);
        else cache->captured_ns=s->captured_ns,cache->sample_token=sample_token;
        g=dm_ta_current_set(s,button,now,skill_set);
        cache->glyph[skill_set][index]=g;cache->present[skill_set][index]=1;
        return g;
    }
    if(!visual_same_state(cache,s,scene_epoch,world_epoch) || sample_token!=cache->sample_token ||
       !cache->present[skill_set][index]) {
        dm_ta_visual_reset(cache);return g;
    }
    return cache->glyph[skill_set][index];
}
