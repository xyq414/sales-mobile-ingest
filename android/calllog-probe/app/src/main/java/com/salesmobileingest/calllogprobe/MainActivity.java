package com.salesmobileingest.calllogprobe;

import android.Manifest;
import android.app.Activity;
import android.content.ContentResolver;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.Build;
import android.provider.CallLog;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;

/**
 * A deliberately narrow debug-only feasibility probe. It has no network, storage,
 * contacts, telephony-state, dialer, background, or write capabilities.
 */
public final class MainActivity extends Activity {
    private static final int REQUEST_READ_CALL_LOG = 100;
    private static final String RESULT_FILE = "probe-result.json";
    private static final int MAX_ROWS = 20;
    private static final long MAX_WINDOW_SECONDS = 7_200L;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        TextView status = new TextView(this);
        status.setText("CallLog feasibility probe running locally");
        setContentView(status);
        if (checkSelfPermission(Manifest.permission.READ_CALL_LOG) == PackageManager.PERMISSION_GRANTED) {
            runProbe();
        } else {
            requestPermissions(new String[]{Manifest.permission.READ_CALL_LOG}, REQUEST_READ_CALL_LOG);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_READ_CALL_LOG) {
            return;
        }
        if (grantResults.length == 1 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            runProbe();
        } else {
            writeBaseResult("DENIED", "NOT_RUN", null, new JSONArray());
        }
    }

    private void runProbe() {
        if (checkSelfPermission(Manifest.permission.READ_CALL_LOG) != PackageManager.PERMISSION_GRANTED) {
            writeBaseResult("DENIED", "NOT_RUN", null, new JSONArray());
            return;
        }
        long targetOccurredAt = getIntent().getLongExtra("targetOccurredAtMillis", -1L);
        long requestedWindow = getIntent().getLongExtra("windowSeconds", 600L);
        long windowSeconds = Math.max(60L, Math.min(MAX_WINDOW_SECONDS, requestedWindow));
        if (targetOccurredAt <= 0L) {
            writeBaseResult("GRANTED", "CONFIGURATION_ERROR", "MissingTargetOccurredAt", new JSONArray());
            return;
        }
        long lowerBound = targetOccurredAt - (windowSeconds * 1_000L);
        long upperBound = targetOccurredAt + (windowSeconds * 1_000L);
        JSONArray rows = new JSONArray();
        try {
            Uri uri = CallLog.Calls.CONTENT_URI.buildUpon()
                    .appendQueryParameter(CallLog.Calls.LIMIT_PARAM_KEY, String.valueOf(MAX_ROWS))
                    .build();
            String[] projection = new String[]{
                    CallLog.Calls.NUMBER,
                    CallLog.Calls.DATE,
                    CallLog.Calls.DURATION,
                    CallLog.Calls.TYPE,
                    CallLog.Calls.CACHED_NAME,
            };
            String selection = CallLog.Calls.DATE + " >= ? AND " + CallLog.Calls.DATE + " <= ?";
            String[] selectionArgs = new String[]{String.valueOf(lowerBound), String.valueOf(upperBound)};
            ContentResolver resolver = getContentResolver();
            try (Cursor cursor = resolver.query(uri, projection, selection, selectionArgs, CallLog.Calls.DATE + " DESC")) {
                if (cursor == null) {
                    writeBaseResult("GRANTED", "EMPTY", null, rows);
                    return;
                }
                int numberIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.NUMBER);
                int dateIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.DATE);
                int durationIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.DURATION);
                int typeIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.TYPE);
                int nameIndex = cursor.getColumnIndexOrThrow(CallLog.Calls.CACHED_NAME);
                while (cursor.moveToNext() && rows.length() < MAX_ROWS) {
                    JSONObject row = new JSONObject();
                    row.put("number", cursor.isNull(numberIndex) ? JSONObject.NULL : cursor.getString(numberIndex));
                    row.put("date_epoch_ms", cursor.getLong(dateIndex));
                    row.put("duration_seconds", cursor.getLong(durationIndex));
                    row.put("type", cursor.getInt(typeIndex));
                    row.put("cached_name", cursor.isNull(nameIndex) ? JSONObject.NULL : cursor.getString(nameIndex));
                    rows.put(row);
                }
            }
            writeBaseResult("GRANTED", rows.length() == 0 ? "EMPTY" : "PASS", null, rows);
        } catch (SecurityException exception) {
            writeBaseResult("GRANTED", "SECURITY_EXCEPTION", exception.getClass().getName(), rows);
        } catch (Exception exception) {
            writeBaseResult("GRANTED", "ERROR", exception.getClass().getName(), rows);
        }
    }

    private void writeBaseResult(String permissionStatus, String queryStatus, String exceptionClass, JSONArray rows) {
        try {
            JSONObject result = new JSONObject();
            result.put("schema_version", "android-calllog-probe-result/v1");
            result.put("package_name", getPackageName());
            result.put("probe_timestamp", Instant.now().toString());
            result.put("api_level", Build.VERSION.SDK_INT);
            result.put("manufacturer", Build.MANUFACTURER);
            result.put("model", Build.MODEL);
            result.put("permission_status", permissionStatus);
            result.put("query_status", queryStatus);
            result.put("query_exception_class", exceptionClass == null ? JSONObject.NULL : exceptionClass);
            result.put("rows", rows);
            File output = new File(getFilesDir(), RESULT_FILE);
            try (FileOutputStream stream = new FileOutputStream(output, false)) {
                stream.write(result.toString().getBytes(StandardCharsets.UTF_8));
                stream.flush();
                stream.getFD().sync();
            }
        } catch (Exception ignored) {
            // No logcat fallback: CallLog values must not leave the app-private result file.
        }
    }
}
