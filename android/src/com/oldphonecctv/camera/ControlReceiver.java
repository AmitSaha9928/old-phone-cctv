package com.oldphonecctv.camera;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;

/**
 * Starts the camera after a reboot, and lets the PC control the app over adb:
 *   am broadcast -n com.oldphonecctv.camera/.ControlReceiver -a com.oldphonecctv.camera.START
 *   am broadcast -n com.oldphonecctv.camera/.ControlReceiver -a com.oldphonecctv.camera.STOP
 *   am broadcast -n com.oldphonecctv.camera/.ControlReceiver -a com.oldphonecctv.camera.SET_KEY --es key ABCD1234
 * Only the system and the adb shell can send these (the manifest requires DUMP).
 */
public class ControlReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        String a = intent.getAction();
        if (a == null) return;
        SharedPreferences prefs = ctx.getSharedPreferences("cfg", Context.MODE_PRIVATE);
        switch (a) {
            case Intent.ACTION_BOOT_COMPLETED:
            case Intent.ACTION_MY_PACKAGE_REPLACED:
                if (prefs.getBoolean("enabled", false)) start(ctx);
                break;
            case "com.oldphonecctv.camera.START":
                start(ctx);
                break;
            case "com.oldphonecctv.camera.STOP":
                ctx.startService(new Intent(ctx, CamService.class).setAction(CamService.ACTION_STOP));
                break;
            case "com.oldphonecctv.camera.SET_KEY": {
                String key = intent.getStringExtra("key");
                if (key != null && key.length() >= 6) prefs.edit().putString("key", key).commit();
                setResultData(prefs.getString("key", ""));
                break;
            }
        }
    }

    static void start(Context ctx) {
        Intent i = new Intent(ctx, CamService.class);
        if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
        else ctx.startService(i);
    }
}
