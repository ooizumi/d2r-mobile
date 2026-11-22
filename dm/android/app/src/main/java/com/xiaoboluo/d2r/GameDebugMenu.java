package com.xiaoboluo.d2r;

import android.app.Activity;
import android.content.Context;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.CompoundButton;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;
import java.util.Locale;

/** Touch-friendly debug menu that leaves the game Surface running beneath a full-screen native View layer. */
final class GameDebugMenu {
    private static final int CATEGORY_PICTURE = 0;
    private static final int CATEGORY_EFFECTS = 1;
    private static final int CATEGORY_PERFORMANCE = 2;
    private static final int CATEGORY_SAVE = 3;
    private static final int[] SCENE_HEIGHTS = {360, 432, 480, 540, 600, 720, 900, 1080};

    private static final int BACKGROUND = Color.rgb(15, 22, 29);
    private static final int PANEL = Color.rgb(27, 37, 46);
    private static final int FIELD = Color.rgb(37, 50, 61);
    private static final int ACCENT = Color.rgb(84, 208, 143);
    private static final int ACCENT_DARK = Color.rgb(10, 39, 27);
    private static final int TEXT = Color.rgb(239, 246, 243);
    private static final int MUTED = Color.rgb(161, 179, 185);
    private static final int WARN = Color.rgb(241, 184, 91);
    private static final int ERROR = Color.rgb(255, 139, 126);

    private static Ui activeUi;
    private static float density = 1f;

    private GameDebugMenu() {}

    static void show(DmActivity activity, Runnable releaseControls, Runnable resumeGame) {
        if (activity == null || activity.isFinishing() || activity.isDestroyed()) return;
        if (Looper.myLooper() != Looper.getMainLooper()) {
            activity.runOnUiThread(() -> show(activity, releaseControls, resumeGame));
            return;
        }
        density = activity.getResources().getDisplayMetrics().density;
        if (activeUi != null) {
            if (activeUi.activity == activity) activeUi.requestClose();
            else {
                activeUi.close();
                show(activity, releaseControls, resumeGame);
            }
            return;
        }
        releaseControls.run();
        new Ui(activity, releaseControls, resumeGame).open();
    }

    static void dismiss(Activity activity) {
        if (activity == null || activeUi == null || activeUi.activity != activity) return;
        if (Looper.myLooper() != Looper.getMainLooper()) {
            activity.runOnUiThread(() -> dismiss(activity));
            return;
        }
        activeUi.close();
    }

    private static final class Ui {
        private final DmActivity activity;
        private final Runnable releaseControls;
        private final Runnable resumeGame;
        private final FrameLayout root;
        private final LinearLayout categories;
        private final LinearLayout content;
        private final TextView status;
        private final Handler handler = new Handler(Looper.getMainLooper());

        private TextView sceneRequestedView;
        private TextView sceneActualView;
        private TextView sceneUiView;
        private TextView gpuView;
        private TextView fpsView;
        private TextView performanceSceneView;
        private TextView performanceUiView;
        private TextView pipelinesView;
        private TextView compileView;
        private TextView drawsView;
        private TextView effectsStatusView;

        private int selectedCategory = CATEGORY_PICTURE;
        private boolean running;
        private double[] previousState;
        private long previousSampleNanos;

        private final Runnable sample = new Runnable() {
            @Override public void run() {
                if (!running || activeUi != Ui.this) return;
                long now = System.nanoTime();
                double seconds = previousSampleNanos == 0 ? 0 : (now - previousSampleNanos) / 1_000_000_000.0;
                double[] state = activity.readDebugState();
                updateReadout(state, seconds);
                if (valid(state)) {
                    previousState = state.clone();
                    previousSampleNanos = now;
                } else {
                    previousState = null;
                    previousSampleNanos = 0;
                }
                handler.postDelayed(this, 1000);
            }
        };

        Ui(DmActivity activity, Runnable releaseControls, Runnable resumeGame) {
            this.activity = activity;
            this.releaseControls = releaseControls;
            this.resumeGame = resumeGame;
            root = new FrameLayout(activity);
            categories = column(activity);
            content = column(activity);
            status = text(activity, "", 13, MUTED);
        }

        void open() {
            root.setBackgroundColor(BACKGROUND);
            root.setClickable(true);
            root.setFocusableInTouchMode(true);
            root.setOnTouchListener((v, event) -> true);
            root.setOnApplyWindowInsetsListener((view, insets) -> {
                android.graphics.Insets safe = insets.getInsets(
                        WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                                | WindowInsets.Type.systemGestures() | WindowInsets.Type.mandatorySystemGestures()
                                | WindowInsets.Type.tappableElement());
                view.setPadding(safe.left, safe.top, safe.right, safe.bottom);
                return insets;
            });

            LinearLayout outer = column(activity);
            outer.setPadding(dp(16), dp(12), dp(16), dp(12));
            outer.setBackground(rounded(PANEL, 18, 0));
            root.addView(outer, new FrameLayout.LayoutParams(-1, -1));

            TextView title = text(activity, activity.getString(R.string.dmAndroidDebugMenuTitle), 24, TEXT);
            title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            outer.addView(title, new LinearLayout.LayoutParams(-1, -2));

            LinearLayout body = new LinearLayout(activity);
            body.setOrientation(LinearLayout.HORIZONTAL);
            LinearLayout.LayoutParams bodyParams = new LinearLayout.LayoutParams(-1, 0, 1);
            bodyParams.topMargin = dp(10);
            outer.addView(body, bodyParams);

            ScrollView categoryScroll = new ScrollView(activity);
            categoryScroll.setFillViewport(true);
            categoryScroll.setBackground(rounded(BACKGROUND, 12, 0));
            categories.setPadding(dp(8), dp(8), dp(8), dp(8));
            categoryScroll.addView(categories);
            body.addView(categoryScroll, new LinearLayout.LayoutParams(dp(174), -1));

            ScrollView contentScroll = new ScrollView(activity);
            contentScroll.setFillViewport(true);
            contentScroll.setClipToPadding(false);
            contentScroll.setPadding(dp(14), dp(4), dp(10), dp(4));
            content.setPadding(0, 0, dp(4), dp(8));
            contentScroll.addView(content);
            LinearLayout.LayoutParams contentParams = new LinearLayout.LayoutParams(0, -1, 1);
            contentParams.leftMargin = dp(10);
            body.addView(contentScroll, contentParams);

            LinearLayout footer = new LinearLayout(activity);
            footer.setGravity(Gravity.CENTER_VERTICAL);
            footer.setPadding(0, dp(10), 0, 0);
            footer.addView(status, new LinearLayout.LayoutParams(0, -2, 1));
            Button resume = button(activity, activity.getString(R.string.dmAndroidDebugContinue), true);
            resume.setOnClickListener(v -> requestClose());
            LinearLayout.LayoutParams resumeParams = new LinearLayout.LayoutParams(dp(176), dp(52));
            resumeParams.leftMargin = dp(12);
            footer.addView(resume, resumeParams);
            outer.addView(footer, new LinearLayout.LayoutParams(-1, -2));

            activeUi = this;
            activity.addContentView(root, new ViewGroup.LayoutParams(-1, -1));
            root.requestFocus();
            buildCategoryButtons();
            selectCategory(CATEGORY_PICTURE);
            running = true;
            double[] state = activity.readDebugState();
            updateReadout(state, 0);
            if (valid(state)) {
                previousState = state.clone();
                previousSampleNanos = System.nanoTime();
            }
            handler.postDelayed(sample, 1000);
        }

        void close() {
            if (!running && activeUi != this && root.getParent() == null) return;
            running = false;
            handler.removeCallbacks(sample);
            if (activeUi == this) activeUi = null;
            ViewGroup parent = root.getParent() instanceof ViewGroup ? (ViewGroup) root.getParent() : null;
            if (parent != null) parent.removeView(root);
            sceneRequestedView = sceneActualView = sceneUiView = null;
            gpuView = fpsView = performanceSceneView = performanceUiView = null;
            pipelinesView = compileView = drawsView = null;
            effectsStatusView = null;
            releaseControls.run();
            if (!activity.isFinishing() && !activity.isDestroyed()) resumeGame.run();
        }

        void requestClose() { close(); }

        private void buildCategoryButtons() {
            categories.removeAllViews();
            addCategoryButton(CATEGORY_PICTURE, R.string.dmAndroidDebugCategoryPicture);
            addCategoryButton(CATEGORY_EFFECTS, R.string.dmAndroidDebugCategoryEffects);
            addCategoryButton(CATEGORY_PERFORMANCE, R.string.dmAndroidDebugCategoryPerformance);
            addCategoryButton(CATEGORY_SAVE, R.string.dmAndroidDebugCategorySave);
        }

        private void addCategoryButton(int category, int labelResource) {
            Button item = button(activity, activity.getString(labelResource), false);
            item.setGravity(Gravity.CENTER_VERTICAL | Gravity.START);
            item.setPadding(dp(16), 0, dp(12), 0);
            item.setOnClickListener(v -> selectCategory(category));
            categories.addView(item, new LinearLayout.LayoutParams(-1, dp(56)));
        }

        private void selectCategory(int category) {
            selectedCategory = category;
            for (int i = 0; i < categories.getChildCount(); i++) {
                View child = categories.getChildAt(i);
                boolean selected = i == category;
                child.setBackground(selected ? rounded(ACCENT, 10, 0) : rounded(Color.TRANSPARENT, 10, 0));
                if (child instanceof Button) ((Button) child).setTextColor(selected ? ACCENT_DARK : TEXT);
            }
            sceneRequestedView = sceneActualView = sceneUiView = null;
            gpuView = fpsView = performanceSceneView = performanceUiView = null;
            pipelinesView = compileView = drawsView = null;
            effectsStatusView = null;
            content.removeAllViews();
            if (category == CATEGORY_PICTURE) renderPicture();
            else if (category == CATEGORY_EFFECTS) renderEffects();
            else if (category == CATEGORY_PERFORMANCE) renderPerformance();
            else renderSave();
            updateReadout(activity.readDebugState(), 0);
        }

        private void renderPicture() {
            TextView help = text(activity, activity.getString(R.string.dmAndroidDebugRenderResolutionHelp), 14, MUTED);
            help.setLineSpacing(0, 1.12f);
            content.addView(help, cardParams());

            TextView heading = text(activity, activity.getString(R.string.dmAndroidDebugRenderResolution), 16, TEXT);
            heading.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            content.addView(heading, cardParams());

            int selected = activity.debugSceneHeight();
            LinearLayout presets = column(activity);
            LinearLayout presetRow = null;
            for (int i = 0; i < SCENE_HEIGHTS.length; i++) {
                if (i % 4 == 0) {
                    presetRow = new LinearLayout(activity);
                    presetRow.setOrientation(LinearLayout.HORIZONTAL);
                    presets.addView(presetRow, topMargin(i == 0 ? 0 : 5));
                }
                int height = SCENE_HEIGHTS[i];
                Button preset = button(activity, activity.getString(R.string.dmAndroidDebugPreset, height), height == selected);
                preset.setMinWidth(0);
                preset.setMinimumWidth(0);
                preset.setTextSize(13);
                preset.setPadding(dp(3), 0, dp(3), 0);
                preset.setOnClickListener(v -> applySceneHeight(height));
                LinearLayout.LayoutParams presetParams = new LinearLayout.LayoutParams(0, dp(42), 1f);
                if (i % 4 > 0) presetParams.leftMargin = dp(5);
                presetRow.addView(preset, presetParams);
            }
            content.addView(presets, cardParams());

            LinearLayout readback = column(activity);
            readback.setPadding(dp(14), dp(10), dp(14), dp(10));
            readback.setBackground(rounded(FIELD, 10, 0));
            sceneRequestedView = text(activity, "", 14, TEXT);
            sceneActualView = text(activity, "", 15, ACCENT);
            sceneUiView = text(activity, "", 14, MUTED);
            sceneActualView.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            readback.addView(sceneRequestedView);
            readback.addView(sceneActualView, topMargin(5));
            readback.addView(sceneUiView, topMargin(5));
            content.addView(readback, cardParams());
            setStatus(activity.getString(R.string.dmAndroidDebugLiveHelp), MUTED);
        }

        private void renderEffects() {
            content.addView(sectionTitle(activity.getString(R.string.dmAndroidDebugEffectsHeading)), cardParams());

            addRenderOptionRow(R.string.dmAndroidDebugBloom, R.string.dmAndroidDebugBloomHelp,
                    DmActivity.RENDER_OPTION_DISABLE_BLOOM, activity.debugBloomEnabled());
            addRenderOptionRow(R.string.dmAndroidDebugPixelUpscale, R.string.dmAndroidDebugPixelUpscaleHelp,
                    DmActivity.RENDER_OPTION_PIXEL_SCENE, activity.debugPixelSceneEnabled());

            effectsStatusView = text(activity,
                    activity.getString(R.string.dmAndroidDebugEffectsUnavailable), 14, TEXT);
            effectsStatusView.setLineSpacing(0, 1.12f);
            effectsStatusView.setPadding(dp(14), dp(10), dp(14), dp(10));
            effectsStatusView.setBackground(rounded(FIELD, 10, 0));
            content.addView(effectsStatusView, cardParams());
            setStatus(activity.getString(R.string.dmAndroidDebugEffectsHelp), MUTED);
        }

        private void addRenderOptionRow(int labelResource, int helpResource, int option, boolean checked) {
            LinearLayout row = column(activity);
            row.setPadding(dp(12), dp(7), dp(12), dp(9));
            row.setBackground(rounded(FIELD, 10, 0));

            Switch toggle = new Switch(activity);
            toggle.setText(activity.getString(labelResource));
            toggle.setTextColor(TEXT);
            toggle.setTextSize(16);
            toggle.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            toggle.setMinHeight(dp(48));
            toggle.setChecked(checked);
            bindRenderOptionSwitch(toggle, option);
            row.addView(toggle, new LinearLayout.LayoutParams(-1, -2));

            TextView help = text(activity, activity.getString(helpResource), 13, MUTED);
            help.setLineSpacing(0, 1.1f);
            row.addView(help, topMargin(1));
            content.addView(row, cardParams());
        }

        private void bindRenderOptionSwitch(Switch toggle, int option) {
            toggle.setOnCheckedChangeListener((button, checked) -> {
                int result = activity.applyDebugRenderOption(option,
                        option == DmActivity.RENDER_OPTION_DISABLE_BLOOM ? !checked : checked);
                if (result != 0) {
                    button.setOnCheckedChangeListener(null);
                    button.setChecked(option == DmActivity.RENDER_OPTION_DISABLE_BLOOM
                            ? activity.debugBloomEnabled() : activity.debugPixelSceneEnabled());
                    bindRenderOptionSwitch(toggle, option);
                    setStatus(activity.getString(R.string.dmAndroidDebugEffectsApplyFailed), ERROR);
                    return;
                }
                setStatus(activity.getString(R.string.dmAndroidDebugEffectsHelp), MUTED);
                updateReadout(activity.readDebugState(), 0);
            });
        }

        private void applySceneHeight(int height) {
            int result = activity.applyDebugSceneHeight(height);
            if (result == 0) {
                renderCategoryContents();
                setStatus(activity.getString(R.string.dmAndroidDebugSceneApplySuccess, height), ACCENT);
            } else {
                setStatus(activity.getString(R.string.dmAndroidDebugSceneApplyFailed, height, result), ERROR);
            }
        }

        private void renderPerformance() {
            content.addView(sectionTitle(activity.getString(R.string.dmAndroidDebugPerformanceHeading)), cardParams());
            TextView heading = text(activity, activity.getString(R.string.dmAndroidDebugLiveSummary), 16, TEXT);
            heading.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            content.addView(heading, cardParams());

            LinearLayout live = column(activity);
            live.setPadding(dp(14), dp(10), dp(14), dp(10));
            live.setBackground(rounded(FIELD, 10, 0));
            fpsView = metric(live, R.string.dmAndroidDebugFps);
            gpuView = metric(live, R.string.dmAndroidDebugGpu);
            performanceSceneView = metric(live, R.string.dmAndroidDebugScene);
            performanceUiView = metric(live, R.string.dmAndroidDebugUi);
            pipelinesView = metric(live, R.string.dmAndroidDebugPipelines);
            compileView = metric(live, R.string.dmAndroidDebugCompile);
            drawsView = metric(live, R.string.dmAndroidDebugDrawsPerFrame);
            content.addView(live, cardParams());

            TextView liveHelp = text(activity, activity.getString(R.string.dmAndroidDebugLiveHelp), 13, MUTED);
            liveHelp.setLineSpacing(0, 1.1f);
            content.addView(liveHelp, cardParams());

            TextView profileHeading = text(activity, activity.getString(R.string.dmAndroidDebugProfileHeading), 16, TEXT);
            profileHeading.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            content.addView(profileHeading, cardParams());
            TextView profileHelp = text(activity, activity.getString(R.string.dmAndroidDebugProfileHelp), 14, MUTED);
            profileHelp.setLineSpacing(0, 1.12f);
            content.addView(profileHelp, cardParams());
            addProfileButton(R.string.dmAndroidProfileCpu, 1);
            addProfileButton(R.string.dmAndroidProfileDescriptors, 2);
            addProfileButton(R.string.dmAndroidProfileDraws, 3);
        }

        private void addProfileButton(int label, int kind) {
            Button button = button(activity, activity.getString(label), false);
            button.setGravity(Gravity.CENTER_VERTICAL | Gravity.START);
            button.setPadding(dp(16), 0, dp(12), 0);
            button.setOnClickListener(v -> {
                int result = activity.requestDebugProfile(kind, 60);
                Toast.makeText(activity, result == 0 ? R.string.dmAndroidProfileStarted
                        : R.string.dmAndroidProfileUnavailable, Toast.LENGTH_SHORT).show();
            });
            content.addView(button, buttonRowParams());
        }

        private void renderSave() {
            content.addView(sectionTitle(activity.getString(R.string.dmAndroidDebugSaveHeading)), cardParams());
            TextView heading = text(activity, activity.getString(R.string.dmAndroidDebugAutosave), 17, TEXT);
            heading.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            content.addView(heading, cardParams());
            TextView help = text(activity, activity.getString(R.string.dmAndroidDebugAutosaveHelp), 14, MUTED);
            help.setLineSpacing(0, 1.12f);
            content.addView(help, cardParams());

            CheckBox autosave = new CheckBox(activity);
            autosave.setText(activity.getString(R.string.dmAndroidDebugAutosave));
            autosave.setTextColor(TEXT);
            autosave.setTextSize(16);
            autosave.setMinHeight(dp(52));
            autosave.setChecked(activity.backgroundAutosaveEnabled());
            autosave.setOnCheckedChangeListener((CompoundButton button, boolean checked) -> {
                activity.applyBackgroundAutosave(checked);
                Toast.makeText(activity, activity.getString(R.string.dmAndroidDebugAutosaveChanged,
                        activity.getString(checked ? R.string.dmAndroidDebugOn : R.string.dmAndroidDebugOff)),
                        Toast.LENGTH_SHORT).show();
            });
            content.addView(autosave, cardParams());
            setStatus(activity.getString(R.string.dmAndroidDebugLiveHelp), MUTED);
        }

        private TextView metric(LinearLayout parent, int labelResource) {
            LinearLayout row = new LinearLayout(activity);
            row.setGravity(Gravity.CENTER_VERTICAL);
            TextView label = text(activity, activity.getString(labelResource), 14, MUTED);
            TextView value = text(activity, activity.getString(R.string.dmAndroidDebugValueUnavailable), 17, ACCENT);
            value.setGravity(Gravity.END);
            value.setTypeface(Typeface.MONOSPACE, Typeface.BOLD);
            row.addView(label, new LinearLayout.LayoutParams(0, -2, 1));
            row.addView(value, new LinearLayout.LayoutParams(-2, -2));
            parent.addView(row, topMargin(parent.getChildCount() == 0 ? 0 : 7));
            return value;
        }

        private void updateReadout(double[] state, double seconds) {
            updateEffectsReadout(state);
            if (!valid(state)) {
                setIfPresent(sceneRequestedView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(sceneActualView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(sceneUiView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(performanceSceneView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(performanceUiView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(gpuView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(fpsView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(pipelinesView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(compileView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(drawsView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                return;
            }

            double requestedHeight = state[0];
            double actualWidth = state[1];
            double actualHeight = state[2];
            String actualSize = dimension(actualWidth, actualHeight);
            String uiSize = dimension(state[3], state[4]);
            String requested = requestedHeight > 0 ? sceneHeight(requestedHeight) : activity.getString(R.string.dmAndroidDebugValueUnavailable);
            double expectedHeight = expectedSceneHeight(requestedHeight, state[4]);
            String actualState;
            if (requestedHeight <= 0 || actualHeight <= 0) actualState = activity.getString(R.string.dmAndroidDebugSceneUnavailable);
            else if (Math.abs(expectedHeight - actualHeight) <= 0.5) actualState = activity.getString(R.string.dmAndroidDebugSceneApplied);
            else actualState = activity.getString(R.string.dmAndroidDebugScenePending);

            setIfPresent(sceneRequestedView, activity.getString(R.string.dmAndroidDebugSceneRequested, requested));
            setIfPresent(sceneActualView, activity.getString(R.string.dmAndroidDebugSceneActual, actualSize, actualState));
            if (sceneActualView != null) sceneActualView.setTextColor(
                    requestedHeight > 0 && actualHeight > 0 && Math.abs(expectedHeight - actualHeight) > 0.5 ? WARN : ACCENT);
            setIfPresent(sceneUiView, activity.getString(R.string.dmAndroidDebugSceneUi, uiSize));
            setIfPresent(performanceSceneView, actualSize);
            setIfPresent(performanceUiView, uiSize);

            if (previousState == null || seconds <= 0) {
                setIfPresent(gpuView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(fpsView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(pipelinesView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(compileView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                setIfPresent(drawsView, activity.getString(R.string.dmAndroidDebugValueUnavailable));
                return;
            }

            double presents = nonNegativeDelta(state[6], previousState[6]);
            double pipelines = nonNegativeDelta(state[7], previousState[7]);
            double compileMs = nonNegativeDelta(state[8], previousState[8]);
            double draws = nonNegativeDelta(state[9], previousState[9]);
            setIfPresent(gpuView, presents > 0 && state[5] >= 0 ? milliseconds(state[5])
                    : activity.getString(R.string.dmAndroidDebugValueUnavailable));
            setIfPresent(fpsView, decimal(presents / seconds));
            setIfPresent(pipelinesView, decimal(pipelines / seconds));
            setIfPresent(compileView, milliseconds(compileMs / seconds));
            setIfPresent(drawsView, presents > 0 ? decimal(draws / presents) : activity.getString(R.string.dmAndroidDebugValueUnavailable));
        }

        private void renderCategoryContents() {
            content.removeAllViews();
            sceneRequestedView = sceneActualView = sceneUiView = null;
            gpuView = fpsView = performanceSceneView = performanceUiView = null;
            pipelinesView = compileView = drawsView = null;
            effectsStatusView = null;
            if (selectedCategory == CATEGORY_PICTURE) renderPicture();
            else if (selectedCategory == CATEGORY_EFFECTS) renderEffects();
            else if (selectedCategory == CATEGORY_PERFORMANCE) renderPerformance();
            else renderSave();
            updateReadout(activity.readDebugState(), 0);
        }

        private void updateEffectsReadout(double[] state) {
            if (effectsStatusView == null) return;
            if (state == null || state.length < 14) {
                effectsStatusView.setText(activity.getString(R.string.dmAndroidDebugEffectsUnavailable));
                effectsStatusView.setTextColor(TEXT);
                return;
            }
            int requested = renderMaskValue(state[10]);
            int applied = renderMaskValue(state[11]);
            if (requested < 0 || applied < 0) {
                effectsStatusView.setText(activity.getString(R.string.dmAndroidDebugEffectsUnavailable));
                effectsStatusView.setTextColor(TEXT);
                return;
            }
            String bloom = activity.getString((applied & DmActivity.RENDER_OPTION_DISABLE_BLOOM) == 0
                    ? R.string.dmAndroidDebugOn : R.string.dmAndroidDebugOff);
            String pixel = activity.getString((applied & DmActivity.RENDER_OPTION_PIXEL_SCENE) != 0
                    ? R.string.dmAndroidDebugOn : R.string.dmAndroidDebugOff);
            String message = activity.getString(R.string.dmAndroidDebugEffectsApplied, bloom, pixel);
            if (requested != applied) {
                message += "\n" + activity.getString(R.string.dmAndroidDebugEffectsPending);
                effectsStatusView.setTextColor(WARN);
            } else {
                message += "\n" + activity.getString(R.string.dmAndroidDebugEffectsSkippedDraws,
                        wholeNumber(state[12]), wholeNumber(state[13]));
                effectsStatusView.setTextColor(TEXT);
            }
            effectsStatusView.setText(message);
        }

        private void setStatus(String message, int color) {
            status.setText(message);
            status.setTextColor(color);
        }

        private TextView sectionTitle(String label) {
            TextView title = text(activity, label, 19, TEXT);
            title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            return title;
        }

        private void setIfPresent(TextView view, String value) {
            if (view != null) view.setText(value);
        }
    }

    private static boolean valid(double[] state) { return state != null && state.length >= 10; }

    private static int renderMaskValue(double value) {
        if (!(value >= 0) || value > 3 || value != Math.rint(value)) return -1;
        return (int)value;
    }

    private static String wholeNumber(double value) {
        return value >= 0 && value < Long.MAX_VALUE ? String.format(Locale.getDefault(), "%.0f", value) : "--";
    }

    private static double nonNegativeDelta(double current, double previous) {
        double delta = current - previous;
        return delta >= 0 ? delta : 0;
    }

    private static String dimension(double width, double height) {
        if (!(width > 0) || !(height > 0)) return "--";
        return Math.round(width) + "×" + Math.round(height);
    }

    private static String sceneHeight(double value) { return Math.round(value) + "p"; }

    private static double expectedSceneHeight(double requested, double outputHeight) {
        return outputHeight > 0 ? Math.min(outputHeight, Math.max(outputHeight / 3.0, requested)) : requested;
    }

    private static String milliseconds(double value) { return String.format(Locale.ROOT, "%.2f ms", value); }

    private static String decimal(double value) { return String.format(Locale.ROOT, "%.1f", value); }

    private static TextView text(Context context, String value, int size, int color) {
        TextView view = new TextView(context);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color);
        return view;
    }

    private static LinearLayout column(Context context) {
        LinearLayout view = new LinearLayout(context);
        view.setOrientation(LinearLayout.VERTICAL);
        return view;
    }

    private static Button button(Context context, String label, boolean primary) {
        Button view = new Button(context);
        view.setText(label);
        view.setTextSize(15);
        view.setAllCaps(false);
        view.setTextColor(primary ? ACCENT_DARK : TEXT);
        view.setMinHeight(dp(50));
        view.setPadding(dp(16), 0, dp(16), 0);
        view.setBackground(rounded(primary ? ACCENT : FIELD, 10, 0));
        return view;
    }

    private static GradientDrawable rounded(int color, int radiusDp, int strokeColor) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radiusDp));
        if (strokeColor != 0) drawable.setStroke(dp(1), strokeColor);
        return drawable;
    }

    private static int dp(int value) { return Math.round(value * density); }

    private static LinearLayout.LayoutParams cardParams() {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.bottomMargin = dp(9);
        return params;
    }

    private static LinearLayout.LayoutParams buttonRowParams() {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(50));
        params.bottomMargin = dp(7);
        return params;
    }

    private static LinearLayout.LayoutParams topMargin(int marginDp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.topMargin = dp(marginDp);
        return params;
    }
}
