import dm from 'libdm.so';
import { DebugSettings, loadDebugSettings, saveDebugSettings, SCENE_HEIGHTS } from '../utils/DebugSettings';

@Component
export struct GameDebugMenu {
  @Prop filesRoot: string = '';
  @Prop fps: string = '';
  onClose: () => void = () => {};
  @State category: number = 0;
  @State values: number[] = [];
  @State selectedHeight: number = 0;
  @State flags: number = 0;
  @State autosave: boolean = true;
  @State message: string = '设置即时生效，并保存在这台设备上。';
  @State drawsPerFrame: string = '—';
  private settings: DebugSettings = new DebugSettings();
  private timer: number = -1;
  private previous: number[] = [];
  private categories: string[] = ['画面', '特效', '性能', '存档'];

  aboutToAppear(): void {
    this.settings = loadDebugSettings(this.filesRoot);
    this.autosave = this.settings.backgroundAutosave;
    this.sample();
    this.timer = setInterval(() => { this.sample(); }, 1000);
  }
  aboutToDisappear(): void { if (this.timer >= 0) { clearInterval(this.timer); } }
  private sample(): void {
    const state = dm.readDebugState();
    if (state.length < 14) { return; }
    if (this.previous.length >= 14 && state[6] > this.previous[6]) {
      this.drawsPerFrame = ((state[9] - this.previous[9]) / (state[6] - this.previous[6])).toFixed(0);
    }
    this.previous = state;
    this.values = state;
    this.selectedHeight = state[0];
    this.flags = state[10];
  }
  private persist(): void {
    this.message = saveDebugSettings(this.filesRoot, this.settings)
      ? '已应用，下次启动继续使用。' : '本次已应用，但设置保存失败。';
  }
  private setHeight(height: number): void {
    const rc = dm.setSceneHeight(height);
    if (rc !== 0) { this.message = `分辨率设置失败（${rc}）`; return; }
    this.settings.sceneHeight = height;
    this.selectedHeight = height;
    this.persist(); this.sample();
  }
  private setOption(mask: number, enabled: boolean): void {
    const next = enabled ? this.flags | mask : this.flags & ~mask;
    if (dm.setRenderOptions(next) !== 0) { this.message = '特效设置失败'; return; }
    this.flags = next; this.settings.renderOptions = next;
    this.persist(); this.sample();
  }
  private setAutosave(enabled: boolean): void {
    dm.setBackgroundAutosave(enabled);
    this.autosave = enabled; this.settings.backgroundAutosave = enabled; this.persist();
  }
  private profile(kind: number): void {
    this.message = dm.requestProfile(kind, 60) === 0
      ? '已开始采样 60 帧，结果写入诊断日志。' : '采样暂不可用，请稍后再试。';
  }
  private sizeAt(offset: number): string {
    return this.values.length >= 14 && this.values[offset] > 0
      ? `${this.values[offset]} × ${this.values[offset + 1]}` : '等待场景…';
  }
  @Builder
  metric(label: string, value: string) {
    Row() {
      Text(label).fontSize(16).fontColor('#a1b3b9').layoutWeight(1)
      Text(value).fontSize(18).fontColor('#54d08f')
    }.width('100%').padding({ top: 5, bottom: 5 })
  }
  @Builder
  picture() {
    Text('3D 场景分辨率').fontSize(21).fontWeight(FontWeight.Bold).fontColor('#eff6f3')
    Text('只调整 3D 场景；界面和小地图使用独立输出分辨率。较低分辨率可降低 GPU 负担和发热。')
      .fontSize(15).fontColor('#a1b3b9')
    Flex({ wrap: FlexWrap.Wrap }) {
      ForEach(SCENE_HEIGHTS, (height: number) => {
        Button(`${height}p`).width(100).height(44).margin({ right: 8, bottom: 8 })
          .backgroundColor(this.selectedHeight === height ? '#54d08f' : '#25323d')
          .fontColor(this.selectedHeight === height ? '#0a271b' : '#eff6f3')
          .onClick(() => { this.setHeight(height); })
      }, (height: number) => `${height}`)
    }.width('100%')
    this.metric('请求高度', this.selectedHeight > 0 ? `${this.selectedHeight}p` : '设备默认')
    this.metric('实际 3D 场景', this.sizeAt(1))
    this.metric('界面 / 输出', this.sizeAt(3))
  }
  @Builder
  effects() {
    Row() {
      Text('泛光（Bloom）').fontSize(18).fontColor('#eff6f3').layoutWeight(1)
      Toggle({ type: ToggleType.Switch, isOn: (this.flags & 1) === 0 })
        .onChange((on: boolean) => { this.setOption(1, !on); })
    }.width('100%')
    Text('控制亮处的光晕。关闭后保留色调映射、界面和文字，不等于关闭全部后处理。')
      .fontSize(15).fontColor('#a1b3b9')
    Row() {
      Text('像素风格放大').fontSize(18).fontColor('#eff6f3').layoutWeight(1)
      Toggle({ type: ToggleType.Switch, isOn: (this.flags & 2) !== 0 })
        .onChange((on: boolean) => { this.setOption(2, on); })
    }.width('100%')
    Text('3D 场景使用最近邻放大，低分辨率下保留像素边缘；界面和文字保持原有处理。')
      .fontSize(15).fontColor('#a1b3b9')
    this.metric('已生效选项', this.values.length >= 14 ? `${this.values[11]}` : '—')
  }
  @Builder
  performance() {
    this.metric('帧率', this.fps)
    this.metric('GPU', this.values.length >= 14 && this.values[5] > 0 ? `${this.values[5].toFixed(2)} ms` : '—')
    this.metric('3D 场景', this.sizeAt(1))
    this.metric('界面 / 输出', this.sizeAt(3))
    this.metric('每帧绘制数', this.drawsPerFrame)
    this.metric('最近一帧新管线', this.values.length >= 14 ? `${this.values[7]}` : '—')
    this.metric('最近一帧管线编译', this.values.length >= 14 ? `${this.values[8].toFixed(2)} ms` : '—')
    Text('短时采样会增加开销，结果写入诊断日志。').fontSize(14).fontColor('#a1b3b9')
    Row({ space: 10 }) {
      Button('CPU 采样').onClick(() => { this.profile(1); })
      Button('描述符采样').onClick(() => { this.profile(2); })
      Button('绘制采样').onClick(() => { this.profile(3); })
    }
  }
  @Builder
  saves() {
    Row() {
      Text('切到后台时自动保存').fontSize(18).fontColor('#eff6f3').layoutWeight(1)
      Toggle({ type: ToggleType.Switch, isOn: this.autosave })
        .onChange((on: boolean) => { this.setAutosave(on); })
    }.width('100%')
    Text('局内切到后台时请求游戏保存。退出游戏前仍建议使用“保存并退出”；系统突然终止应用时无法保证完成。')
      .fontSize(15).fontColor('#a1b3b9')
    Text('这是本机游戏保存开关。云端同步在本地提交完成后另行处理。')
      .fontSize(15).fontColor('#a1b3b9')
  }
  build() {
    Column({ space: 16 }) {
      Row() {
        Text('暗黑2 · 调试菜单').fontSize(26).fontWeight(FontWeight.Bold).fontColor('#eff6f3').layoutWeight(1)
        Text(this.fps).fontColor('#54d08f').fontSize(18)
      }.width('100%')
      Row({ space: 20 }) {
        Column({ space: 10 }) {
          ForEach(this.categories, (label: string, index: number) => {
            Button(label).width('100%').height(52)
              .backgroundColor(this.category === index ? '#54d08f' : '#25323d')
              .fontColor(this.category === index ? '#0a271b' : '#eff6f3')
              .onClick(() => { this.category = index; })
          }, (label: string) => label)
        }.width(140).height('100%')
        Scroll() {
          Column({ space: 16 }) {
            if (this.category === 0) { this.picture() }
            else if (this.category === 1) { this.effects() }
            else if (this.category === 2) { this.performance() }
            else { this.saves() }
          }.width('100%').alignItems(HorizontalAlign.Start).padding(16)
        }.layoutWeight(1).height('100%').align(Alignment.TopStart)
      }.layoutWeight(1).width('100%')
      Row({ space: 16 }) {
        Text(this.message).fontSize(14).fontColor('#a1b3b9').layoutWeight(1)
        Button('继续游戏').width(160).height(48).backgroundColor('#54d08f').fontColor('#0a271b')
          .onClick(() => { this.onClose(); })
      }.width('100%')
    }.width('100%').height('100%').padding(24).backgroundColor('#0f161d')
      .hitTestBehavior(HitTestMode.Default).id('d2r-debug-menu')
  }
}
