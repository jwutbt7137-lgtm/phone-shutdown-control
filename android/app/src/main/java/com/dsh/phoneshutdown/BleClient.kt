package com.dsh.phoneshutdown

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import java.util.UUID

/**
 * 与电脑端 BLE 服务通信的最小客户端。
 * 所有回调都切回主线程，方便直接更新界面。
 */
class BleClient(private val context: Context, private val listener: Listener) {

    interface Listener {
        fun onScanResult(name: String, address: String, isTarget: Boolean, rssi: Int)
        fun onScanStopped(scanTotal: Int, markedCount: Int)
        fun onConnected(deviceName: String?)
        fun onDisconnected(reason: String)
        fun onReady()
        fun onMessage(text: String)
        fun onLog(text: String)

        /** 自动试连过程中的进度 */
        fun onProbeProgress(tried: Int, total: Int, current: String)
    }

    companion object {
        private const val TAG = "BleClient"
        private const val CONNECT_TIMEOUT_MS = 15_000L
        private const val READY_TIMEOUT_MS = 10_000L
        private const val SCAN_TIMEOUT_MS = 12_000L
        private const val AUTO_STOP_DELAY_MS = 1_500L
    }

    private val main = Handler(Looper.getMainLooper())
    private val serviceUuid: UUID = UUID.fromString(BleProtocol.SERVICE_UUID)
    private val rxUuid: UUID = UUID.fromString(BleProtocol.CHAR_RX_UUID)
    private val txUuid: UUID = UUID.fromString(BleProtocol.CHAR_TX_UUID)
    private val cccdUuid: UUID = UUID.fromString(BleProtocol.CCCD_UUID)

    private val bluetoothManager: BluetoothManager? =
        context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
    private val adapter: BluetoothAdapter? get() = bluetoothManager?.adapter
    private val scanner: BluetoothLeScanner? get() = adapter?.bluetoothLeScanner

    private var gatt: BluetoothGatt? = null
    private var rxChar: BluetoothGattCharacteristic? = null
    private var txChar: BluetoothGattCharacteristic? = null

    var isScanning: Boolean = false
        private set
    var isConnected: Boolean = false
        private set

    private var connectTimeout: Runnable? = null
    private var readyTimeout: Runnable? = null
    private var scanTimeout: Runnable? = null
    private var autoStopPending = false
    private var readySignaled = false
    private val seenAddresses = HashSet<String>()

    /** 本次扫描中，通过广播证据认出是"关机服务"的设备数量 */
    private var markedCount = 0

    /** 已认出的电脑地址（同一个地址只计数一次，避免刷屏） */
    private val markedAddresses = HashSet<String>()

    /**
     * 用户曾成功连过的电脑名（小写）。
     *
     * 为什么需要它：Windows 的 BLE 广播用的是**轮换随机地址**，实测
     * RAINBOW 的地址几分钟内就从 6B:D9:B0:C3:69:B4 变成 48:58:15:36:0C:45，
     * 而且 Windows 不允许第三方设置广播名、广播里也解析不出自定义标识，
     * 所以"记住电脑的蓝牙名"是最可靠的识别方式。
     */
    private var knownPcNames: Set<String> = emptySet()

    fun setKnownPcNames(names: Collection<String>) {
        knownPcNames = names.map { it.lowercase().trim() }.filter { it.isNotEmpty() }.toSet()
        Log.d(TAG, "已记住的电脑名：$knownPcNames")
    }

    /** 本次扫描累计发现的设备总数（用于诊断） */
    private var scanTotal = 0

    /** 扫描过的设备快照，供界面提示用 */
    private val scanSnapshot = LinkedHashMap<String, Pair<String, Int>>()

    /** 是否在找电脑的过程中（自动逐个尝试连接） */
    private var probeQueue: List<Pair<String, Pair<String, Int>>> = emptyList()
    private var probeIndex = 0

    /** 当前正在试连的地址；连上后若没有我们的服务就自动换下一个 */
    private var probeAddress: String? = null

    /** 自动试连时的候选上限，避免挨个连几十个设备 */
    private val maxProbe = 8

    val isProbing: Boolean get() = probeAddress != null

    /**
     * 扫描结束后调用：按信号从强到弱，逐个试连附近设备，
     * 谁的 GATT 服务里有我们的 UUID，谁就是电脑。
     *
     * 这是"广播里认不出来"时的兜底手段 —— 不依赖广播内容，直接问设备本身。
     */
    fun startAutoFind() {
        val candidates = scanSnapshot.entries
            .filter { !markedAddresses.contains(it.key) }
            .sortedByDescending { it.value.second }
            .take(maxProbe)
            .map { it.key to it.value }

        if (candidates.isEmpty()) {
            listener.onLog("没有可尝试的设备。请先扫描，并确认电脑端程序在运行")
            return
        }
        probeQueue = candidates
        probeIndex = 0
        listener.onLog("开始逐个试连 ${candidates.size} 个设备，找带关机服务的那个…")
        probeNext()
    }

    fun cancelAutoFind() {
        if (probeAddress == null && probeQueue.isEmpty()) return
        probeAddress = null
        probeQueue = emptyList()
        probeIndex = 0
        disconnect(notify = false)
        listener.onLog("已停止自动寻找")
    }

    @SuppressLint("MissingPermission")
    private fun probeNext() {
        if (probeIndex >= probeQueue.size) {
            probeAddress = null
            probeQueue = emptyList()
            listener.onLog("试完所有候选设备，都没找到关机服务。请确认电脑端程序正在运行")
            listener.onScanStopped(scanTotal, markedCount)
            return
        }
        val (address, info) = probeQueue[probeIndex]
        probeIndex++
        probeAddress = address
        listener.onProbeProgress(probeIndex, probeQueue.size, info.first)
        listener.onLog("试连第 ${probeIndex}/${probeQueue.size} 个：${info.first}  $address")
        connectInternal(address)
    }

    /**
     * 判断一条广播是不是我们的电脑发出的。
     *
     * 用多条证据交叉判断，命中任意一条即认定：
     *  1. 广播名就是 PC-Shutdown
     *  2. 广播里带着自定义标识段 PSD1
     *  3. 广播的服务 UUID 列表里有我们的主服务 UUID
     *  4. 广播里出现我们服务的 16 位短 UUID（a001）
     *
     * 之所以要这么多条：Windows 不允许第三方设置广播名（实测 0x09/0x08
     * 数据段都返回"拒绝访问"），能用的只有数据段与服务 UUID，多一条证据
     * 就多一分认出来的机会。
     */
    private fun looksLikeOurPc(record: android.bluetooth.le.ScanRecord?, name: String?): Boolean {
        // 证据 1：名字就是约定的广播名
        if (BleProtocol.ADVERTISED_NAME.equals(name, ignoreCase = true)) return true
        // 证据 1b：这个名字是用户曾成功连接过的电脑（最实用的一条）
        if (!name.isNullOrBlank() && knownPcNames.contains(name.lowercase().trim())) return true
        if (record == null) return false

        // 证据 2：自定义标识段
        val mfr = record.manufacturerSpecificData
        if (mfr != null) {
            for (i in 0 until mfr.size()) {
                val payload = mfr.valueAt(i) ?: continue
                if (containsMarker(payload)) return true
            }
        }
        val svcData = try {
            record.serviceData
        } catch (e: Exception) {
            null
        }
        if (svcData != null) {
            for (entry in svcData.values) {
                if (containsMarker(entry)) return true
            }
        }

        // 证据 3：完整服务 UUID
        val uuids = try {
            record.serviceUuids
        } catch (e: Exception) {
            null
        }
        if (uuids != null) {
            for (u in uuids) {
                if (u == serviceUuid) return true
            }
        }

        // 证据 4：16 位短 UUID（Windows 广播 GATT 服务时常用短格式）
        val raw = try {
            record.bytes
        } catch (e: Exception) {
            null
        }
        if (raw != null && raw.size >= 5) {
            var i = 0
            while (i < raw.size) {
                val len = raw[i].toInt() and 0xFF
                if (len == 0 || i + len >= raw.size) break
                val type = raw[i + 1].toInt() and 0xFF
                if (type == 0x02 || type == 0x03) {
                    var j = i + 2
                    while (j + 1 < i + 1 + len) {
                        val short = (raw[j].toInt() and 0xFF) or ((raw[j + 1].toInt() and 0xFF) shl 8)
                        if (short == 0xA001) return true
                        j += 2
                    }
                }
                i += len + 1
            }
        }
        return false
    }

    /** 在字节流里找标识，允许出现在任意合理位置（容错不同厂商的封装） */
    private fun containsMarker(bytes: ByteArray): Boolean {
        val marker = BleProtocol.ADVERT_MARKER
        if (bytes.size < marker.size) return false
        val limit = bytes.size - marker.size
        for (start in 0..limit) {
            var hit = true
            for (i in marker.indices) {
                if (bytes[start + i] != marker[i]) {
                    hit = false
                    break
                }
            }
            if (hit) return true
        }
        return false
    }

    // ------------------------------------------------------------------ 权限

    fun requiredPermissions(): Array<String> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            arrayOf(
                android.Manifest.permission.BLUETOOTH_SCAN,
                android.Manifest.permission.BLUETOOTH_CONNECT
            )
        } else {
            arrayOf(android.Manifest.permission.ACCESS_FINE_LOCATION)
        }

    fun hasPermissions(): Boolean = requiredPermissions().all {
        context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED
    }

    // ------------------------------------------------------------------ 扫描

    @SuppressLint("MissingPermission")
    fun startScan() {
        if (!hasPermissions()) {
            listener.onLog("缺少蓝牙权限，无法扫描")
            return
        }
        val a = adapter
        if (a == null) {
            listener.onLog("这台手机没有蓝牙适配器")
            return
        }
        if (!a.isEnabled) {
            listener.onLog("蓝牙未开启，请先打开蓝牙")
            return
        }
        if (isScanning) return

        val s = scanner
        if (s == null) {
            listener.onLog("无法获取 BLE 扫描器")
            return
        }

        seenAddresses.clear()
        markedAddresses.clear()
        scanSnapshot.clear()
        markedCount = 0
        scanTotal = 0
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()

        try {
            s.startScan(null, settings, scanCallback)
            isScanning = true
            autoStopPending = false
            // 扫描不能永远开着：低延迟模式很费电，锁屏后系统还可能静默停扫
            val r = Runnable { if (isScanning) stopScan() }
            scanTimeout = r
            main.postDelayed(r, SCAN_TIMEOUT_MS)
            listener.onLog("开始扫描附近设备…（电脑端程序需先运行）")
        } catch (e: SecurityException) {
            listener.onLog("扫描失败：权限被拒绝 (${e.message})")
        }
    }

    @SuppressLint("MissingPermission")
    fun stopScan() {
        val wasScanning = isScanning
        isScanning = false
        scanTimeout?.let { main.removeCallbacks(it) }
        scanTimeout = null
        try {
            scanner?.stopScan(scanCallback)
        } catch (e: SecurityException) {
            Log.w(TAG, "stopScan denied", e)
        }
        if (wasScanning) listener.onScanStopped(scanTotal, markedCount)
    }

    private val scanCallback = object : ScanCallback() {
        @SuppressLint("MissingPermission")
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val device: BluetoothDevice = result.device
            val address = device.address ?: return
            val name = try {
                result.scanRecord?.deviceName ?: device.name
            } catch (e: SecurityException) {
                null
            }
            val isTarget = looksLikeOurPc(result.scanRecord, name)
            val display = when {
                !name.isNullOrBlank() -> name
                isTarget -> "电脑（未报名字）"
                else -> "(未命名设备)"
            }
            val rssi = result.rssi
            scanSnapshot[address] = display to rssi

            if (isTarget) {
                // 认出是电脑：BLE 扫描每秒回调几十次，只在首次认出时上报一次，
                // 否则日志会被刷屏（曾把关键信息全顶掉）。
                if (markedAddresses.add(address)) {
                    markedCount++
                    listener.onScanResult(display, address, true, rssi)
                    // 已经找到目标，再扫下去只是耗电；稍等一下让上层决定连不连
                    if (!autoStopPending) {
                        autoStopPending = true
                        main.postDelayed({ if (isScanning) stopScan() }, AUTO_STOP_DELAY_MS)
                    }
                }
            } else if (seenAddresses.add(address)) {
                scanTotal++
                listener.onScanResult(display, address, false, rssi)
            }
        }

        override fun onScanFailed(errorCode: Int) {
            isScanning = false
            scanTimeout?.let { main.removeCallbacks(it) }
            scanTimeout = null
            listener.onLog("扫描失败，错误码 $errorCode")
            listener.onScanStopped(scanTotal, markedCount)
        }
    }

    // ------------------------------------------------------------------ 连接

    /** 用户主动连接：连上后如果没有我们的服务，就报错而不是继续试下一个。 */
    fun connect(address: String) {
        probeAddress = null
        probeQueue = emptyList()
        probeIndex = 0
        connectInternal(address)
    }

    @SuppressLint("MissingPermission")
    private fun connectInternal(address: String) {
        if (!hasPermissions()) {
            listener.onLog("缺少蓝牙权限，无法连接")
            if (isProbing) probeNext()
            return
        }
        val a = adapter ?: run {
            listener.onLog("这台手机没有蓝牙适配器")
            if (isProbing) probeNext()
            return
        }
        stopScan()
        disconnect(notify = false)

        val device: BluetoothDevice = try {
            a.getRemoteDevice(address)
        } catch (e: IllegalArgumentException) {
            listener.onLog("设备地址无效：$address")
            if (isProbing) probeNext()
            return
        }

        listener.onLog("正在连接 $address …")
        val g: BluetoothGatt? = try {
            device.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
        } catch (e: SecurityException) {
            listener.onLog("连接被拒绝：${e.message}")
            null
        }

        if (g == null) {
            listener.onLog("连接失败：系统未能创建 GATT 客户端")
            if (isProbing) probeNext()
            return
        }
        gatt = g
        armConnectTimeout(address, g)
    }

    private fun armConnectTimeout(address: String, g: BluetoothGatt) {
        cancelConnectTimeout()
        val r = Runnable {
            if (gatt === g && !isConnected) {
                if (isProbing) {
                    Log.d(TAG, "试连超时：$address")
                    probeNext()
                } else {
                    listener.onLog("连接超时（15 秒）。请确认电脑端程序在运行，且手机在蓝牙范围内")
                    disconnect(notify = true)
                }
            }
        }
        connectTimeout = r
        main.postDelayed(r, CONNECT_TIMEOUT_MS)
    }

    /**
     * 连上之后还要发现服务、订阅通知，这一段以前没有超时：
     * CCCD 写入没回调时会永远停在"握手中"。这里补一个就绪超时。
     */
    private fun armReadyTimeout(g: BluetoothGatt) {
        cancelReadyTimeout()
        val r = Runnable {
            if (gatt === g && !readySignaled) {
                listener.onLog("通道就绪超时（${READY_TIMEOUT_MS / 1000} 秒）。请重试，或换一台电脑试试")
                disconnect(notify = true)
            }
        }
        readyTimeout = r
        main.postDelayed(r, READY_TIMEOUT_MS)
    }

    private fun cancelReadyTimeout() {
        readyTimeout?.let { main.removeCallbacks(it) }
        readyTimeout = null
    }

    /**
     * 自动试连时，这台设备不是电脑（没有我们的服务）。
     * 安静地换下一个，不打扰用户。
     */
    private fun probeFail(reason: String) {
        val address = probeAddress
        Log.d(TAG, "试连失败（$reason）：$address")
        disconnect(notify = false)
        if (probeAddress != null) probeNext()
    }

    private fun cancelConnectTimeout() {
        connectTimeout?.let { main.removeCallbacks(it) }
        connectTimeout = null
    }

    @SuppressLint("MissingPermission")
    fun disconnect(notify: Boolean = true) {
        cancelConnectTimeout()
        cancelReadyTimeout()
        readySignaled = false
        val g = gatt
        gatt = null
        rxChar = null
        txChar = null
        val wasConnected = isConnected
        isConnected = false
        if (g != null) {
            try {
                g.disconnect()
            } catch (e: SecurityException) {
                Log.w(TAG, "disconnect denied", e)
            }
            try {
                g.close()
            } catch (e: Exception) {
                Log.w(TAG, "close failed", e)
            }
        }
        if (notify && wasConnected) listener.onDisconnected("已断开")
    }

    private val gattCallback = object : BluetoothGattCallback() {

        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            main.post {
                // 迟到的旧连接回调不能污染/关掉新连接
                if (g !== gatt) {
                    runCatching { g.close() }
                    return@post
                }
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    listener.onLog("连接出错（GATT 状态 $status）")
                }
                when (newState) {
                    BluetoothProfile.STATE_CONNECTED -> {
                        cancelConnectTimeout()
                        isConnected = true
                        armReadyTimeout(g)
                        val name = try {
                            g.device.name
                        } catch (e: SecurityException) {
                            null
                        }
                        listener.onConnected(name ?: g.device.address)
                        listener.onLog("已连接，正在发现服务…")
                        val started = try {
                            g.discoverServices()
                        } catch (e: SecurityException) {
                            listener.onLog("发现服务失败：${e.message}")
                            false
                        }
                        if (!started) {
                            listener.onLog("发起服务发现失败，正在断开重试")
                            disconnect(notify = true)
                        }
                    }

                    BluetoothProfile.STATE_DISCONNECTED -> {
                        isConnected = false
                        disconnect(notify = false)
                        if (isProbing) {
                            probeNext()
                        } else {
                            listener.onDisconnected(
                                if (status == BluetoothGatt.GATT_SUCCESS) "连接已断开"
                                else "连接异常断开（状态 $status）"
                            )
                        }
                    }
                }
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            main.post {
                if (g !== gatt) {
                    runCatching { g.close() }
                    return@post
                }
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    if (isProbing) {
                        probeFail("服务发现失败 $status")
                    } else {
                        listener.onLog("服务发现失败（状态 $status）")
                        disconnect(notify = true)
                    }
                    return@post
                }
                val service = g.getService(serviceUuid)
                if (service == null) {
                    if (isProbing) {
                        probeFail("不是电脑")
                    } else {
                        listener.onLog("这台设备上没有关机服务，不是目标电脑——请在扫描列表里换一个试试")
                        disconnect(notify = true)
                    }
                    return@post
                }
                val rx = service.getCharacteristic(rxUuid)
                val tx = service.getCharacteristic(txUuid)
                if (rx == null || tx == null) {
                    if (isProbing) {
                        probeFail("特征不全")
                    } else {
                        listener.onLog("服务特征不完整，可能是电脑端版本过旧")
                        disconnect(notify = true)
                    }
                    return@post
                }
                // 找到了：结束试连状态，正常往下走
                if (isProbing) listener.onLog("找到了！这台就是电脑")
                probeAddress = null
                probeQueue = emptyList()
                probeIndex = 0
                rxChar = rx
                txChar = tx

                try {
                    val ok = g.setCharacteristicNotification(tx, true)
                    if (!ok) {
                        listener.onLog("打开通知失败")
                        disconnect(notify = true)
                        return@post
                    }
                    val cccd = tx.getDescriptor(cccdUuid)
                    if (cccd == null) {
                        listener.onLog("缺少通知配置描述符")
                        disconnect(notify = true)
                        return@post
                    }
                    cccd.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                    @Suppress("DEPRECATION")
                    val writeStarted = g.writeDescriptor(cccd)
                    if (!writeStarted) {
                        listener.onLog("订阅通知失败，正在断开重试")
                        disconnect(notify = true)
                    }
                } catch (e: SecurityException) {
                    listener.onLog("配置通知失败：${e.message}")
                    disconnect(notify = true)
                }
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
            main.post {
                if (g !== gatt) {
                    runCatching { g.close() }
                    return@post
                }
                if (status == BluetoothGatt.GATT_SUCCESS) {
                    readySignaled = true
                    cancelReadyTimeout()
                    listener.onReady()
                } else {
                    listener.onLog("通知配置失败（状态 $status）")
                    disconnect(notify = true)
                }
            }
        }

        @Deprecated("Android 13 以下走这个回调")
        override fun onCharacteristicChanged(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
            if (g !== gatt) return
            @Suppress("DEPRECATION")
            val data = characteristic.value ?: return
            deliver(data)
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray
        ) {
            if (g !== gatt) return
            deliver(value)
        }

        // 注意：Android SDK 里 onCharacteristicWrite 只有这一个 3 参重载，
        // 不存在带 byte[] 的 4 参版本（那是 onCharacteristicRead 才有的）。
        //
        // 状态 14 (GATT_ERROR) 在 Windows 服务端上属于"应答环节"的已知噪音：
        // 改用 WRITE_TYPE_NO_RESPONSE 后通常不再出现；万一还出现，
        // 只要通知通道正常收到了应答，就不该吓唬用户，故只记一条提示。
        @Deprecated("Android 13 以下走这个回调")
        override fun onCharacteristicWrite(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                Log.w(TAG, "write callback status=$status（若已收到应答可忽略）")
                main.post {
                    if (status == 14) {
                        listener.onLog("提示：写入回执异常（状态 14），但应答正常，可忽略")
                    } else {
                        listener.onLog("发送失败（状态 $status）")
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------------ 收发

    private val rxBuffer = java.io.ByteArrayOutputStream()

    private fun deliver(value: ByteArray) {
        main.post {
            // 按字节累积到换行再整行解码：MTU 较小时一条中文消息会被拆成多段，
            // 逐段 String(..., UTF_8) 会解出替换字符。
            rxBuffer.write(value)
            val bytes = rxBuffer.toByteArray()
            var start = 0
            for (i in bytes.indices) {
                if (bytes[i] == '\n'.code.toByte()) {
                    val line = String(bytes, start, i - start, Charsets.UTF_8).trim()
                    if (line.isNotEmpty()) listener.onMessage(line)
                    start = i + 1
                }
            }
            rxBuffer.reset()
            if (start < bytes.size) rxBuffer.write(bytes, start, bytes.size - start)
            // 防御：万一电脑端忘了换行，避免缓冲区无限增长
            if (rxBuffer.size() > 8192) rxBuffer.reset()
        }
    }

    /** 发送一行命令；未连接时返回 false。 */
    @SuppressLint("MissingPermission")
    fun send(command: String): Boolean {
        val g = gatt ?: return false
        val rx = rxChar ?: return false
        if (!isConnected) return false

        val payload = (command + "\n").toByteArray(Charsets.UTF_8)
        return try {
            // 用 WriteWithoutResponse：
            // Windows 端的 GATT 服务对本机的 WriteRequest 应答会失败
            // （"已提交对象"），表现为手机侧反复收到 GATT_ERROR(14)。
            // 改成不需要应答的写入后，这条错误路径根本不会触发，
            // 而电脑端照样能收到数据、照样会回通知。
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                g.writeCharacteristic(
                    rx, payload, BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
                ) == BluetoothGatt.GATT_SUCCESS
            } else {
                @Suppress("DEPRECATION")
                rx.writeType = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
                @Suppress("DEPRECATION")
                rx.value = payload
                @Suppress("DEPRECATION")
                g.writeCharacteristic(rx)
            }
        } catch (e: SecurityException) {
            listener.onLog("发送被拒绝：${e.message}")
            false
        }
    }
}
