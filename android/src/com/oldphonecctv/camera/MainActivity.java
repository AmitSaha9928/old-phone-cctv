package com.oldphonecctv.camera;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** One-screen setup/status page. Everything else is controlled from the PC viewer. */
public class MainActivity extends Activity {
    static final int BG = 0xFF0E1116, CARD = 0xFF171B22, TEXT = 0xFFE8EAED, DIM = 0xFF8A93A0, GREEN = 0xFF3DDC97, RED = 0xFFFF6B6B;

    TextView state, details;
    Button toggle, battery;
    final Handler h = new Handler(Looper.getMainLooper());
    final Runnable tick = new Runnable() {
        @Override public void run() { refresh(); h.postDelayed(this, 1000); }
    };

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().setStatusBarColor(BG);
        getWindow().setNavigationBarColor(BG);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BG);
        int p = dp(24);
        root.setPadding(p, dp(40), p, p);

        TextView title = text("OldPhone CCTV", 30, TEXT);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(title);
        TextView sub = text("This phone is a camera for your OldPhone CCTV viewer.", 15, DIM);
        sub.setPadding(0, dp(4), 0, dp(24));
        root.addView(sub);

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(20), dp(18), dp(20), dp(18));
        GradientDrawable bgd = new GradientDrawable();
        bgd.setColor(CARD);
        bgd.setCornerRadius(dp(16));
        card.setBackground(bgd);
        state = text("", 20, TEXT);
        state.setTypeface(Typeface.DEFAULT_BOLD);
        card.addView(state);
        details = text("", 15, DIM);
        details.setPadding(0, dp(10), 0, 0);
        details.setLineSpacing(dp(4), 1f);
        card.addView(details);
        root.addView(card);

        toggle = button("");
        toggle.setOnClickListener(v -> onToggle());
        root.addView(toggle, buttonParams(dp(24)));

        battery = button("Allow running in the background");
        battery.setOnClickListener(v -> askBatteryExemption());
        root.addView(battery, buttonParams(dp(12)));

        Button unpair = button("Reset pairing");
        unpair.setOnClickListener(v -> {
            getSharedPreferences("cfg", MODE_PRIVATE).edit().remove("key").commit();
            android.widget.Toast.makeText(this, "Pairing reset. The next PC that connects will be trusted.", android.widget.Toast.LENGTH_LONG).show();
        });
        root.addView(unpair, buttonParams(dp(12)));

        TextView hint = text("You can turn the screen off. The camera keeps running, and starts again by itself after a reboot.", 14, DIM);
        hint.setPadding(0, dp(24), 0, 0);
        root.addView(hint);

        setContentView(root);
        handleIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleIntent(intent);
    }

    void handleIntent(Intent i) {
        if (i != null && i.getBooleanExtra("autostart", false) && hasPermissions()) start();
    }

    @Override protected void onResume() { super.onResume(); h.post(tick); }
    @Override protected void onPause() { super.onPause(); h.removeCallbacks(tick); }

    void refresh() {
        SharedPreferences prefs = getSharedPreferences("cfg", MODE_PRIVATE);
        boolean on = CamService.running;
        String cs = CamService.camState;
        if (!on) { state.setText("○  Camera off"); state.setTextColor(DIM); }
        else if ("streaming".equals(cs)) { state.setText("●  Camera on"); state.setTextColor(GREEN); }
        else if ("error".equals(cs)) { state.setText("●  Camera problem"); state.setTextColor(RED); }
        else { state.setText("●  " + cs); state.setTextColor(TEXT); }

        StringBuilder sb = new StringBuilder();
        String ip = CamService.wifiIp();
        sb.append("Phone address   ").append(ip == null ? "no Wi-Fi" : ip + ":" + CamService.PORT).append('\n');
        sb.append("Viewer          ").append(CamService.viewer == null ? "not connected" : CamService.viewer).append('\n');
        sb.append("Camera name     ").append(prefs.getString("name", Build.MODEL)).append('\n');
        sb.append("Paired with PC  ").append(prefs.getString("key", null) != null ? "yes" : "no — the first PC that connects pairs");
        if (CamService.lastError != null) sb.append("\n\n").append(CamService.lastError);
        details.setText(sb.toString());
        toggle.setText(on ? "Stop camera" : "Start camera");

        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        battery.setVisibility(pm.isIgnoringBatteryOptimizations(getPackageName()) ? View.GONE : View.VISIBLE);
    }

    void onToggle() {
        if (CamService.running) {
            startService(new Intent(this, CamService.class).setAction(CamService.ACTION_STOP));
        } else if (hasPermissions()) {
            start();
        } else {
            requestPermissions(new String[]{Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO}, 1);
        }
    }

    void start() {
        ControlReceiver.start(this);
    }

    boolean hasPermissions() {
        return checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (hasPermissions()) start();
    }

    void askBatteryExemption() {
        try {
            startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName())));
        } catch (Exception e) {
            startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        }
    }

    // ---------------------------------------------------------------- tiny view helpers

    TextView text(String s, int sp, int color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        return t;
    }

    Button button(String s) {
        Button b = new Button(this);
        b.setText(s);
        b.setAllCaps(false);
        b.setTextSize(16);
        b.setTextColor(BG);
        b.setGravity(Gravity.CENTER);
        GradientDrawable d = new GradientDrawable();
        d.setColor(GREEN);
        d.setCornerRadius(dp(14));
        b.setBackground(d);
        b.setStateListAnimator(null);
        return b;
    }

    LinearLayout.LayoutParams buttonParams(int top) {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(54));
        lp.topMargin = top;
        return lp;
    }

    int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }
}
