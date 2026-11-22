package com.xiaoboluo.d2r;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.view.View;
import java.util.Locale;

/** Counts game presents, independently of the Android UI/display refresh rate. */
final class PerformanceOverlay extends View {
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final float density;
    private String label = "FPS --";
    private long previousTime, previousFrames;
    private final Runnable sample = new Runnable() {
        @Override public void run() {
            if (!isAttachedToWindow() || getWindowVisibility() != VISIBLE) return;
            long now = System.nanoTime(), frames = DmActivity.nativePresentedFrames();
            double seconds = (now - previousTime) / 1e9;
            double fps = seconds > 0 ? Math.max(0, frames - previousFrames) / seconds : 0;
            label = fps > 0 ? String.format(Locale.ROOT, "FPS %.1f  |  %.1f ms", fps, 1000 / fps) : "FPS 0.0";
            previousTime = now; previousFrames = frames;
            invalidate();
            postDelayed(this, 1000);
        }
    };

    PerformanceOverlay(Context context) {
        super(context);
        density = getResources().getDisplayMetrics().density;
        paint.setTextSize(13 * getResources().getDisplayMetrics().scaledDensity);
        setClickable(false); setFocusable(false);
        setImportantForAccessibility(IMPORTANT_FOR_ACCESSIBILITY_NO);
    }

    private void restartSampling() {
        removeCallbacks(sample);
        if (!isAttachedToWindow() || getWindowVisibility() != VISIBLE) return;
        previousTime = System.nanoTime(); previousFrames = DmActivity.nativePresentedFrames();
        label = "FPS --"; invalidate(); postDelayed(sample, 1000);
    }
    @Override protected void onAttachedToWindow() { super.onAttachedToWindow(); restartSampling(); }
    @Override protected void onDetachedFromWindow() { removeCallbacks(sample); super.onDetachedFromWindow(); }
    @Override protected void onWindowVisibilityChanged(int visibility) { super.onWindowVisibilityChanged(visibility); restartSampling(); }
    @Override protected void onMeasure(int width, int height) {
        setMeasuredDimension(resolveSize((int)Math.ceil(paint.measureText("FPS 120.0  |  999.9 ms") + 16*density), width),
                resolveSize((int)Math.ceil(paint.getFontSpacing() + 12*density), height));
    }
    @Override protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        paint.setColor(0xB0000000);
        canvas.drawRoundRect(0, 0, getWidth(), getHeight(), 5*density, 5*density, paint);
        paint.setColor(Color.WHITE);
        canvas.drawText(label, 8*density, (getHeight() - paint.ascent() - paint.descent()) / 2, paint);
    }
}
