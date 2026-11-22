#include "dm_touch_native_art.h"
#include "../../host/dm_input.h"
#include <stdio.h>
#include <string.h>
static unsigned checks;
#define CHECK(x) do { checks++; if(!(x)){fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#x);return 1;} } while(0)
static dm_tg_snapshot visual_snapshot(uint64_t captured) {
    const uint32_t ids[DM_TG_SLOTS]={0,115,219,220,359,395};
    const uint32_t logicals[DM_TG_FACETS]={0x1000,0x2000,0x4000,0x8000,0x200,0x800};
    dm_tg_snapshot s={0};
    s.supported=s.valid=s.ready=1;s.source=DM_TG_SOURCE_NATIVE_GETTER;
    s.identity_scope=DM_TG_IDENTITY_NATIVE;s.captured_ns=captured;s.selected_bank=0;
    s.scene_epoch=7;s.world_epoch=9;s.binding_identity=0x1234;s.caller_identity=0x5678;s.hud_identity=0x9abc;
    for(int i=0;i<DM_TG_FACETS;i++)s.mapping[i]=(dm_tg_mapping){logicals[i],(uint32_t)i,(uint32_t)(i+6)};
    s.mapping[0].normal=1;s.mapping[1].normal=0;
    for(int i=0;i<DM_TG_SLOTS;i++)s.raw_skill_id[i]=i<6?ids[i]:UINT32_MAX;
    return s;
}
int main(void) {
    dm_ta_glyph g;
    const uint32_t ids[]={0,115,219,220,359,395};
    const int classes[]={8,3,8,8,8,7},frames[]={2,26,18,18,18,46};
    for(unsigned i=0;i<6;i++) {
        g=dm_ta_skill(ids[i]);CHECK(g.status==DM_TA_GLYPH);CHECK(g.raw_skill_id==ids[i]);
        CHECK(g.atlas==classes[i]);CHECK(g.normal_frame==frames[i]);CHECK(g.pressed_frame==frames[i]+1);
    }
    CHECK(dm_ta_skill(UINT32_MAX).status==DM_TA_EMPTY);
    CHECK(dm_ta_skill(1000000).status==DM_TA_UNKNOWN);
    dm_ta_frame f;CHECK(dm_ta_frame_at(8,18,&f));CHECK(f.x==1224&&f.y==0&&f.width==65&&f.height==65);
    CHECK(dm_ta_frame_at(8,39,&f));CHECK(!dm_ta_frame_at(8,40,&f));CHECK(!dm_ta_frame_at(9,0,&f));CHECK(!dm_ta_frame_at(0,-1,&f));
    CHECK(dm_ta_frame_at(7,59,&f));CHECK(!dm_ta_frame_at(7,60,&f));
    dm_tg_snapshot s={0};s.supported=s.valid=s.ready=1;s.source=1;s.identity_scope=1;s.captured_ns=1000;s.selected_bank=0;
    const uint32_t logicals[]={0x1000,0x2000,0x4000,0x8000,0x200,0x800};
    for(int i=0;i<6;i++){s.mapping[i]=(dm_tg_mapping){logicals[i],(uint32_t)i,(uint32_t)(i+6)};s.raw_skill_id[i]=ids[i];}
    /* 按原runtime tuple而非数组顺序：A normal0/B1/X3/Y2/R4。 | By the original runtime tuple rather than array order: A normal0/B1/X3/Y2/R4. */
    s.mapping[0].normal=1;s.mapping[1].normal=0;
    const uint64_t buttons[]={DM_BTN_A,DM_BTN_B,DM_BTN_X,DM_BTN_Y,DM_BTN_R};
    const int slots[]={0,1,3,2,4};
    for(int i=0;i<5;i++){g=dm_ta_current(&s,buttons[i],1000);CHECK(g.status==DM_TA_GLYPH);CHECK(g.slot==slots[i]);CHECK(g.raw_skill_id==ids[slots[i]]);CHECK(g.effective_slot==slots[i]);}
    /* 银行16所复制的12 ID数组独立，不能退回银行0图。 | The 12-ID array copied by bank 16 is independent and must not fall back to the bank 0 image. */
    s.selected_bank=16;for(int i=0;i<12;i++)s.raw_skill_id[i]=UINT32_MAX;
    g=dm_ta_current(&s,DM_BTN_A,1000);CHECK(g.status==DM_TA_EMPTY);CHECK(g.slot==0&&g.effective_slot==16);
    s.raw_skill_id[0]=395;g=dm_ta_current(&s,DM_BTN_A,1000);CHECK(g.atlas==7&&g.normal_frame==46&&g.effective_slot==16);
    s.raw_skill_id[0]=1000000;CHECK(dm_ta_current(&s,DM_BTN_A,1000).status==DM_TA_UNKNOWN);
    s.raw_skill_id[0]=0;
    CHECK(dm_ta_current(&s,DM_BTN_A,1000+DM_TG_MAX_AGE_NS).status==DM_TA_GLYPH);
    CHECK(dm_ta_current(&s,DM_BTN_A,1001+DM_TG_MAX_AGE_NS).status==DM_TA_UNKNOWN);
    CHECK(dm_ta_current(&s,DM_BTN_A,999).status==DM_TA_UNKNOWN);
    s.ready=0;CHECK(dm_ta_current(&s,DM_BTN_A,1000).status==DM_TA_UNKNOWN);s.ready=1;
    s.valid=0;CHECK(dm_ta_current(&s,DM_BTN_A,1000).status==DM_TA_UNKNOWN);s.valid=1;
    s.supported=0;CHECK(dm_ta_current(&s,DM_BTN_A,1000).status==DM_TA_UNKNOWN);s.supported=1;
    s.source=0;CHECK(dm_ta_current(&s,DM_BTN_A,1000).status==DM_TA_UNKNOWN);s.source=1;
    s.identity_scope=2;CHECK(dm_ta_current(&s,DM_BTN_A,1000).status==DM_TA_UNKNOWN);s.identity_scope=1;
    s.captured_ns=0;CHECK(dm_ta_current(&s,DM_BTN_A,1000).status==DM_TA_UNKNOWN);s.captured_ns=1000;
    s.selected_bank=1;CHECK(dm_ta_current(&s,DM_BTN_A,1000).status==DM_TA_UNKNOWN);s.selected_bank=0;
    CHECK(dm_ta_current(&s,DM_BTN_A|DM_BTN_B,1000).status==DM_TA_UNKNOWN);

    dm_ta_visual_cache cache={0};
    dm_tg_snapshot visual=visual_snapshot(1000);
    uint64_t sample=40,scene=visual.scene_epoch,world=visual.world_epoch;
    g=dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,1000,sample,scene,world,0);
    CHECK(g.status==DM_TA_GLYPH&&g.raw_skill_id==0);
    uint64_t stale=1001+DM_TG_MAX_AGE_NS;
    CHECK(dm_ta_current(&visual,DM_BTN_A,stale).status==DM_TA_UNKNOWN);
    g=dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,stale,sample,scene,world,0);
    CHECK(g.status==DM_TA_GLYPH&&g.raw_skill_id==0&&g.slot==0);
    CHECK(dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,stale,sample+1,scene,world,0).status==DM_TA_UNKNOWN);

    sample++;visual.captured_ns=stale;
    g=dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,stale,sample,scene,world,0);
    CHECK(g.status==DM_TA_GLYPH&&g.raw_skill_id==0);
    CHECK(dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,stale+DM_TG_MAX_AGE_NS+1,sample,scene+1,world,0).status==DM_TA_UNKNOWN);
    scene++;visual.scene_epoch=scene;visual.captured_ns=2000;visual.binding_identity++;
    CHECK(dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,2000,sample,scene,world,0).status==DM_TA_GLYPH);
    CHECK(dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,2000+DM_TG_MAX_AGE_NS+1,sample,scene,world+1,0).status==DM_TA_UNKNOWN);
    world++;visual.world_epoch=world;visual.captured_ns=3000;visual.binding_identity++;
    CHECK(dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,3000,sample,scene,world,0).status==DM_TA_GLYPH);
    visual.selected_bank=16;visual.binding_identity++;
    CHECK(dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,3000+DM_TG_MAX_AGE_NS+1,sample,scene,world,0).status==DM_TA_UNKNOWN);
    visual.selected_bank=0;visual.captured_ns=4000;visual.binding_identity++;
    CHECK(dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,4000,sample,scene,world,0).status==DM_TA_GLYPH);
    visual.binding_identity++;
    CHECK(dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,4000+DM_TG_MAX_AGE_NS+1,sample,scene,world,0).status==DM_TA_UNKNOWN);
    visual.captured_ns=5000;visual.mapping[1].normal=1;visual.binding_identity++;
    g=dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,5000,sample,scene,world,0);
    CHECK(g.status==DM_TA_GLYPH&&g.raw_skill_id==115);
    g=dm_ta_visual_current_set(&cache,&visual,DM_BTN_A,5000+DM_TG_MAX_AGE_NS+1,sample,scene,world,0);
    CHECK(g.status==DM_TA_GLYPH&&g.raw_skill_id==115);

    printf("PASS native_art: %u/%u checks; official IDs6; tuple facets5; banks0/16; frame bounds; visual stall retention/sample+scene+world+bank+identity invalidation; input freshness\n",checks,checks);return 0;
}
