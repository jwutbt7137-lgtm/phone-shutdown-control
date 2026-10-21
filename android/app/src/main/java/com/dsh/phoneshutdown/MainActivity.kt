package com.dsh.phoneshutdown

import android.content.Context
import android.content.SharedPreferences
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity(), BleClient.Listener {

    private companion object {
        const val PREFS = "phone_shutdown"
        const val KEY_PIN = "pin"
        const val KEY_LAST_ADDR = "last_address"
        const val KEY_PC_NAMES = "pc_names"
        const val MAX_LOG_LINES = 200
        const val MAX_SCAN_ROWS = 40
        const val MAX_KNOWN_NAMES = 8
        val PIN_REGEX = Regex("^\\d{4,12}$")
    }

    private lateinit var prefs: SharedPreferences
    private lateinit var ble: BleClient

    private lateinit var statusChip: TextView
    private lateinit var deviceLabel: TextView
    private lateinit var targetRow: LinearLayout
    private lateinit var otherBox: LinearLayout
    private lateinit var otherHeader: TextView
    private lateinit var logView: TextView
    private lateinit var scanBtn: MaterialButton
    private lateinit var connectBtn: MaterialButton
    private lateinit var disconnectBtn: MaterialButton
    private lateinit var pinBtn: MaterialButton
    private lateinit var findBtn: MaterialButton

    /** 本次扫描到的设备名，用于日志里给用户看 */
    private val foundDevices = mutableListOf<String>()

    private val cmdButtons = mutableListOf<MaterialButton>()
    private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    /** 通道就绪前到达的命令先排队，就绪后按序发出 */
    private val outbox = ArrayDeque<String>()
    private var ready = false
    private var scanning = false
    private var pinDialog: AlertDialog? = null
    private var helloHandled = false

    private var targetAddress: String? = null
    private var targetName: String? = null

    /** 电脑端下发的认证 nonce（协议 v2）；旧版电脑端为 null */
    private var authNonce: String? = null

    // ------------------------------------------------------------------ 生命周期

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        ble = BleClient(this, this)

        val root = buildUi()
        setContentView(root)
        // targetSdk 35 在 Android 15 上强制 edge-to-edge，自己给根布局加安全区内边距，
        // 否则状态栏/挖孔会压住标题，底部也可能被导航条盖住。
        WindowCompat.setDecorFitsSystemWindows(window, false)
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        applyStatus("未连接", R.color.text_sub)
        setCommandsEnabled(false)
        log("准备就绪。电脑端需 Windows 10 1809 或更高版本。")

        knownPcNames = prefs.getStringSet(KEY_PC_NAMES, emptySet())?.toMutableSet() ?: mutableSetOf()
        ble.setKnownPcNames(knownPcNames)
        if (knownPcNames.isNotEmpty()) {
            log("已记住的电脑：${knownPcNames.joinToString("、")}")
        }
        val savedAddr = prefs.getString(KEY_LAST_ADDR, null)
        if (savedAddr != null) connectBtn.isEnabled = true

        if (!ble.hasPermissions()) {
            log("首次使用需要授予「附近的设备」权限")
            requestBlePermissions()
        }

        // 有记住的电脑就直接开工，省得每次手动扫描
        if (savedAddr != null || knownPcNames.isNotEmpty()) {
            mainHandler.postDelayed({
                if (!ble.isConnected && !scanning) {
                    log("自动扫描，寻找已记住的电脑…")
                    startScanInternal()
                }
            }, 1200)
        }
    }

    private val mainHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private var knownPcNames: MutableSet<String> = mutableSetOf()

    /** 本轮扫描结束后是否要自动连上认出的电脑 */
    private var autoConnectPending = false

    /** 自动连接是否已经发起（防止扫描停止事件被重复触发时重复连接） */
    private var autoConnectInFlight = false

    /** 认证成功后，把当前这台电脑的名字记下来，下次靠它自动识别。 */
    private fun recordKnownPc(name: String?) {
        if (name.isNullOrBlank()) return
        val cleaned = name.trim()
        if (cleaned.isEmpty() || cleaned == "电脑（未报名字）") return
        if (!knownPcNames.add(cleaned) && knownPcNames.size <= MAX_KNOWN_NAMES) return
        while (knownPcNames.size > MAX_KNOWN_NAMES) {
            knownPcNames.remove(knownPcNames.first())
        }
        prefs.edit().putStringSet(KEY_PC_NAMES, knownPcNames).apply()
        ble.setKnownPcNames(knownPcNames)
    }

    override fun onDestroy() {
        mainHandler.removeCallbacksAndMessages(null)
        if (::ble.isInitialized) {
            // 只断 GATT 会留下扫描回调、自动试连队列和延时任务，
            // 它们继续持有 Activity 引用，且会在后台耗电。
            ble.stopScan()
            ble.cancelAutoFind()
            ble.disconnect(notify = false)
        }
        pinDialog?.dismiss()
        pinDialog = null
        super.onDestroy()
    }

    private fun requestBlePermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !ble.hasPermissions()) {
            requestPermissions(ble.requiredPermissions(), 1001)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != 1001) return
        if (ble.hasPermissions()) {
            log("权限已授予，可以扫描了")
        } else {
            log("权限被拒绝：请到「设置 → 应用 → 电脑关机 → 权限」允许「附近的设备」")
        }
    }

    // ------------------------------------------------------------------ BleClient 回调

    override fun onScanResult(name: String, address: String, isTarget: Boolean, rssi: Int) {
        if (isTarget) {
            targetAddress = address
            targetName = name
            renderTarget(name, address, rssi)
            if (!ble.isConnected) log("认出电脑：$name  $address  (${rssi}dBm)")
        } else {
            addOtherDevice(name, address, rssi)
        }
    }

    override fun onScanStopped(scanTotal: Int, markedCount: Int) {
        scanning = false
        scanBtn.text = "扫描电脑"
        log("扫描结束：共发现 $scanTotal 个设备，认出电脑 $markedCount 个")

        // 认出电脑了就直接连，不用用户再点一次
        val addr = targetAddress
        if (autoConnectPending && addr != null && !ble.isConnected && !autoConnectInFlight) {
            autoConnectInFlight = true
            log("自动连接 ${targetName ?: "电脑"} …")
            prefs.edit().putString(KEY_LAST_ADDR, addr).apply()
            ble.connect(addr)
        }
        autoConnectPending = false

        if (markedCount == 0 && targetAddress == null) {
            if (scanTotal == 0) {
                log("一个设备都没扫到：请确认手机蓝牙已开、「附近的设备」权限已允许")
            } else {
                val sample = foundDevices.take(6).joinToString("、")
                log("扫到的设备：$sample")
                log("没认出电脑。点「自动找电脑」可逐个试连（不依赖广播内容）")
            }
        }
        // 只有真认出电脑才启用"连接电脑"，避免用户点了连到不相干的设备
        connectBtn.isEnabled = targetAddress != null
        findBtn.isEnabled = scanTotal > 0 && markedCount == 0
    }

    override fun onProbeProgress(tried: Int, total: Int, current: String) {
        findBtn.text = "寻找中 $tried/$total"
    }

    override fun onConnected(deviceName: String?) {
        helloHandled = false
        authNonce = null
        pinDialog?.dismiss()
        pinDialog = null
        applyStatus("握手中…", R.color.warn_amber)
        deviceLabel.text = deviceName ?: "已连接"
        pinBtn.isEnabled = true
        log("已连接：${deviceName ?: "未知设备"}")
    }

    override fun onReady() {
        ready = true
        helloHandled = false
        applyStatus("未认证", R.color.warn_amber)
        log("通道就绪，发送握手")
        enqueue(BleProtocol.CMD_HELLO)
    }

    override fun onDisconnected(reason: String) {
        ready = false
        outbox.clear()
        autoConnectInFlight = false
        applyStatus("未连接", R.color.text_sub)
        deviceLabel.text = "未连接"
        pinBtn.isEnabled = false
        setCommandsEnabled(false)
        log(reason)
    }

    override fun onLog(text: String) {
        log(text)
    }

    override fun onMessage(text: String) {
        log("← $text")
        val parts = text.trim().split(" ").filter { it.isNotEmpty() }
        if (parts.isEmpty()) return

        when (parts[0].uppercase()) {
            "HELLO" -> handleHello(parts)

            "OK" -> {
                applyStatus("已认证", R.color.ok_green)
                setCommandsEnabled(true)
                if (parts.size > 1 && parts[1].equals("AUTH", ignoreCase = true)) {
                    log("认证通过，可以发送关机命令了")
                    recordKnownPc(targetName)
                }
            }

            "ERR" -> {
                val detail = parts.drop(1).joinToString(" ")
                if (detail.contains("AUTH", ignoreCase = true)) {
                    applyStatus("认证失败", R.color.danger_red)
                    setCommandsEnabled(false)
                    log("PIN 不正确：请对照电脑端窗口里显示的 PIN 重新输入")
                    askForPin(force = true)
                } else {
                    log("电脑端报错：$detail")
                }
            }

            "STATE" -> log("电脑状态：" + parts.drop(1).joinToString(" "))
            "PONG" -> log("电脑端在线，链路正常")
        }
    }

    private fun handleHello(parts: List<String>) {
        // 电脑端在订阅时推一次 HELLO，手机自己发 HELLO 的应答又是一次，
        // 不设防会叠出两个 PIN 输入框 / 重复认证。
        if (helloHandled) return
        helloHandled = true

        val version = parts.getOrNull(1)?.toIntOrNull() ?: 0
        val needAuth = parts.getOrNull(2)?.equals("AUTH", ignoreCase = true) ?: true

        // 协议 v2：电脑端下发 nonce，手机回 HMAC，PIN 不上链路
        authNonce = parts.getOrNull(3)?.takeIf { it.isNotBlank() }

        if (version != BleProtocol.PROTOCOL_VERSION) {
            log("提示：协议版本不一致（电脑端 $version，手机端 ${BleProtocol.PROTOCOL_VERSION}）")
        }
        if (!needAuth) {
            applyStatus("已认证", R.color.ok_green)
            setCommandsEnabled(true)
            log("电脑端未启用 PIN 校验")
            return
        }

        // 只有"和上次认证过的同一台设备（按蓝牙地址）"才自动发 PIN。
        // 广播名和 PSD1 标识都能伪造，不能拿来当信任依据。
        val saved = prefs.getString(KEY_PIN, null)
        val savedAddr = prefs.getString(KEY_LAST_ADDR, null)
        val currentAddr = targetAddress ?: savedAddr
        val trusted = savedAddr != null && currentAddr != null &&
            savedAddr.equals(currentAddr, ignoreCase = true)
        if (saved != null && PIN_REGEX.matches(saved) && trusted) {
            log(
                if (authNonce != null) "用已保存的 PIN 认证（同一台电脑，HMAC）"
                else "用已保存的 PIN 认证（旧版电脑端，明文 PIN）"
            )
            sendAuth(saved)
        } else {
            if (saved != null) log("换了设备，请确认设备名称和地址后再输入 PIN")
            applyStatus("需要认证", R.color.warn_amber)
            askForPin(force = false)
        }
    }

    // ------------------------------------------------------------------ PIN 输入

    private fun askForPin(force: Boolean) {
        if (isFinishing || isDestroyed) return
        if (pinDialog?.isShowing == true) return
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
            hint = "电脑端窗口显示的 PIN"
            val saved = prefs.getString(KEY_PIN, null)
            if (!force && saved != null) setText(saved)
            setSelection(text.length)
        }
        val holder = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(8), dp(24), 0)
            addView(
                input,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                )
            )
        }

        val dialog = AlertDialog.Builder(this)
            .setTitle(if (force) "PIN 不正确，请重新输入" else "输入电脑端显示的 PIN")
            .setMessage(
                "当前设备：${targetName ?: "未知"}（${targetAddress ?: "未知地址"}）\n" +
                    "电脑端程序窗口里有一行「本次 PIN」，照抄即可。输入一次后本机自动记住。"
            )
            .setView(holder)
            .setCancelable(false)
            .setPositiveButton("确定", null)
            .setNegativeButton("稍后") { _, _ ->
                log("已跳过认证")
                applyStatus("需要认证", R.color.warn_amber)
            }
            .create()

        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val value = input.text.toString().trim()
                if (!PIN_REGEX.matches(value)) {
                    input.error = "请输入 4-12 位数字"
                    return@setOnClickListener
                }
                prefs.edit().putString(KEY_PIN, value).apply()
                dialog.dismiss()
                sendAuth(value)
            }
        }
        dialog.setOnDismissListener { if (pinDialog === dialog) pinDialog = null }
        pinDialog = dialog
        dialog.show()
    }

    // ------------------------------------------------------------------ 命令

    /** 日志区可以截图、可以复制，PIN 不能原样写进去。 */
    private fun redact(command: String): String =
        if (command.startsWith(BleProtocol.CMD_AUTH, ignoreCase = true)) {
            "${BleProtocol.CMD_AUTH} ****"
        } else {
            command
        }

    /** 有 nonce 就走协议 v2 的 HMAC 应答，没有就退回明文 PIN（旧版电脑端）。 */
    private fun sendAuth(pin: String) {
        val nonce = authNonce
        if (nonce != null) {
            enqueue("${BleProtocol.CMD_AUTH2} ${BleProtocol.hmacHex(pin, nonce)}")
        } else {
            enqueue("${BleProtocol.CMD_AUTH} $pin")
        }
    }

    private fun enqueue(command: String) {
        if (!ble.isConnected) {
            log("尚未连接电脑，无法发送")
            return
        }
        if (!ready) {
            outbox.addLast(command)
            log("排队等待通道就绪：${redact(command)}")
            return
        }
        if (ble.send(command)) log("→ ${redact(command)}") else log("发送失败：${redact(command)}")
    }

    private fun drainOutbox() {
        while (ready && outbox.isNotEmpty()) {
            val cmd = outbox.removeFirst()
            if (ble.send(cmd)) log("→ ${redact(cmd)}") else log("发送失败：${redact(cmd)}")
        }
    }

    private fun confirm(title: String, message: String, command: String) {
        AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage(message)
            .setNegativeButton("取消", null)
            .setPositiveButton("确定") { _, _ -> enqueue(command) }
            .show()
    }

    private fun doConnect() {
        val addr = targetAddress ?: prefs.getString(KEY_LAST_ADDR, null)
        if (addr == null) {
            log("还没有目标电脑，请先点「扫描电脑」")
            return
        }
        if (!ble.hasPermissions()) {
            requestBlePermissions()
            return
        }
        outbox.clear()
        ready = false
        autoConnectPending = false
        autoConnectInFlight = true
        prefs.edit().putString(KEY_LAST_ADDR, addr).apply()
        ble.connect(addr)
    }

    // ------------------------------------------------------------------ 界面

    private fun startScanInternal() {
        if (scanning) return
        if (!ble.hasPermissions()) {
            requestBlePermissions()
            return
        }
        otherBox.removeAllViews()
        otherHeader.visibility = View.GONE
        targetRow.removeAllViews()
        foundDevices.clear()
        // 保留"上次连过的电脑"地址，扫描没有新结果时「连接电脑」仍然可用
        targetAddress = prefs.getString(KEY_LAST_ADDR, null)
        autoConnectPending = true
        autoConnectInFlight = false
        connectBtn.isEnabled = false
        findBtn.isEnabled = false
        scanning = true
        scanBtn.text = "停止扫描"
        ble.startScan()
    }

    private fun buildUi(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(c(R.color.page_bg))
        }
        root.addView(header())

        val scroll = ScrollView(this).apply {
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)
            isFillViewport = true
        }
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(12), dp(14), dp(24))
        }
        scroll.addView(content)
        root.addView(scroll)

        content.addView(deviceCard())
        content.addView(commandCard())
        content.addView(logCard())
        return root
    }

    private fun header(): View = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setBackgroundColor(c(R.color.brand_primary))
        setPadding(dp(18), dp(16), dp(18), dp(16))
        addView(text("电脑关机", 20f, Color.WHITE, bold = true))
        addView(
            text("手机通过蓝牙把电脑关掉（关机 / 重启 / 睡眠）", 12f, 0xCCFFFFFF.toInt())
                .apply { setPadding(0, dp(3), 0, 0) }
        )
    }

    private fun deviceCard(): View {
        val box = cardBody()
        box.addView(statusRow())

        deviceLabel = text("未连接", 15f, c(R.color.text_main))
            .apply { setPadding(0, dp(6), 0, dp(10)) }
        box.addView(deviceLabel)

        scanBtn = actionButton("扫描电脑") {
            if (scanning) {
                ble.stopScan()
                scanning = false
                scanBtn.text = "扫描电脑"
                return@actionButton
            }
            startScanInternal()
        }
        connectBtn = actionButton("连接电脑") {
            if (ble.isConnected) {
                ble.disconnect()
                ready = false
                outbox.clear()
                setCommandsEnabled(false)
            } else {
                doConnect()
            }
        }
        connectBtn.isEnabled = false

        findBtn = actionButton("自动找电脑") {
            if (ble.isProbing) {
                ble.cancelAutoFind()
                findBtn.text = "自动找电脑"
            } else {
                ble.startAutoFind()
            }
        }
        findBtn.isEnabled = false

        pinBtn = actionButton("手动输入 PIN") { askForPin(force = false) }
        pinBtn.isEnabled = false

        disconnectBtn = actionButton("断开") {
            ble.disconnect()
            ready = false
            outbox.clear()
            setCommandsEnabled(false)
        }
        disconnectBtn.isEnabled = false

        box.addView(row(scanBtn, connectBtn))
        box.addView(row(findBtn, pinBtn))
        box.addView(row(disconnectBtn))

        targetRow = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(8), 0, 0)
        }
        box.addView(targetRow)

        otherHeader = text("附近的其他设备（点一下可连接）", 12f, c(R.color.text_sub))
            .apply {
                setPadding(0, dp(12), 0, dp(4))
                visibility = View.GONE
            }
        box.addView(otherHeader)

        otherBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        box.addView(otherBox)
        return wrapCard("连接", box)
    }

    private fun commandCard(): View {
        val box = cardBody()
        box.addView(text("关机与重启会先弹确认框，避免误触。", 12f, c(R.color.text_sub)))

        box.addView(
            row(
                commandButton("关机") { confirm("关机", "确定要关闭电脑吗？", BleProtocol.CMD_SHUTDOWN) },
                commandButton("重启") { confirm("重启", "确定要重启电脑吗？", BleProtocol.CMD_RESTART) }
            )
        )
        box.addView(
            row(
                commandButton("睡眠") { enqueue(BleProtocol.CMD_SLEEP) },
                commandButton("取消关机") { enqueue(BleProtocol.CMD_CANCEL) }
            )
        )
        box.addView(
            row(
                commandButton("查询状态") { enqueue(BleProtocol.CMD_QUERY) },
                commandButton("测试连通") { enqueue(BleProtocol.CMD_PING) }
            )
        )
        return wrapCard("操作", box)
    }

    private fun logCard(): View {
        val box = cardBody()
        logView = text("", 11f, c(R.color.text_main)).apply {
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
        }
        box.addView(logView)
        return wrapCard("日志", box)
    }

    private fun statusRow(): View = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        statusChip = text("未连接", 12f, Color.WHITE).apply {
            setPadding(dp(10), dp(4), dp(10), dp(4))
        }
        addView(statusChip)
    }

    private fun renderTarget(name: String, address: String, rssi: Int) {
        targetRow.removeAllViews()
        targetRow.addView(
            text("★ $name    $address    ${rssi}dBm", 14f, c(R.color.ok_green), bold = true)
        )
        targetRow.addView(
            text("点「连接电脑」即可使用", 11f, c(R.color.text_sub))
                .apply { setPadding(0, dp(2), 0, 0) }
        )
        connectBtn.isEnabled = true
    }

    private fun addOtherDevice(name: String, address: String, rssi: Int) {
        foundDevices.add(name)
        if (otherBox.childCount >= MAX_SCAN_ROWS) return
        otherHeader.visibility = View.VISIBLE
        otherBox.addView(
            text("$name    $address    ${rssi}dBm", 12f, c(R.color.text_sub)).apply {
                setPadding(0, dp(4), 0, dp(4))
                setOnClickListener {
                    prefs.edit().putString(KEY_LAST_ADDR, address).apply()
                    targetAddress = address
                    targetName = name
                    log("选择设备：$name  $address")
                    ble.connect(address)
                }
            }
        )
    }

    // ------------------------------------------------------------------ View 小工具

    private fun text(value: String, size: Float, color: Int, bold: Boolean = false): TextView =
        TextView(this).apply {
            text = value
            textSize = size
            setTextColor(color)
            if (bold) typeface = Typeface.DEFAULT_BOLD
        }

    /** 卡片内部的内容容器 */
    private fun cardBody(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
    }

    /** 把内容容器包成「标题 + 卡片」的外层容器 */
    private fun wrapCard(title: String, body: LinearLayout): View {
        val outer = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = dp(12) }
        }
        outer.addView(
            text(title, 13f, c(R.color.brand_primary), bold = true)
                .apply { setPadding(0, 0, 0, dp(8)) }
        )
        outer.addView(
            MaterialCardView(this).apply {
                radius = dp(12).toFloat()
                cardElevation = dp(1).toFloat()
                setContentPadding(dp(14), dp(12), dp(14), dp(14))
                addView(
                    body,
                    ViewGroup.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT
                    )
                )
            },
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        )
        return outer
    }

    private fun actionButton(label: String, action: () -> Unit): MaterialButton =
        MaterialButton(this).apply {
            text = label
            isAllCaps = false
            setOnClickListener { action() }
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }

    private fun commandButton(label: String, action: () -> Unit): MaterialButton =
        actionButton(label, action).also { b ->
            cmdButtons.add(b)
            b.isEnabled = false
        }

    private fun row(vararg views: View): View = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        setPadding(0, dp(6), 0, 0)
        views.forEachIndexed { index, v ->
            (v.layoutParams as? LinearLayout.LayoutParams)?.marginStart =
                if (index == 0) 0 else dp(8)
            addView(v)
        }
    }

    private fun applyStatus(value: String, colorRes: Int) {
        statusChip.text = value
        statusChip.setBackgroundColor(c(colorRes))
        if (value == "已认证") connectBtn.text = "重新连接" else connectBtn.text = "连接电脑"
        disconnectBtn.isEnabled = ble.isConnected
    }

    private fun setCommandsEnabled(enabled: Boolean) {
        cmdButtons.forEach { it.isEnabled = enabled }
        if (enabled) drainOutbox()
    }

    private fun log(line: String) {
        val stamp = timeFormat.format(Date())
        val existing = logView.text?.toString().orEmpty()
        val all = if (existing.isEmpty()) listOf() else existing.split("\n")
        logView.text = (all + "[$stamp] $line").takeLast(MAX_LOG_LINES).joinToString("\n")
    }

    private fun dp(value: Int): Int = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, value.toFloat(), resources.displayMetrics
    ).toInt()

    private fun c(colorRes: Int): Int = ContextCompat.getColor(this, colorRes)
}
