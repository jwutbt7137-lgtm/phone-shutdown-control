package com.dsh.phoneshutdown

/**
 * 私有协议常量：手机(中央设备) <-> 电脑(外围设备) 之间的 GATT 契约。
 *
 * 传输格式：UTF-8 文本，每条消息以 '\n' 结尾。
 * 所有 UUID 均为自定义 128 位 UUID。
 */
object BleProtocol {

    /** 主服务 */
    val SERVICE_UUID: String = "0000a001-1e4b-4f6a-9c3d-2b7e5a8f0c11"

    /** 手机 -> 电脑：写入命令（Write Without Response） */
    val CHAR_RX_UUID: String = "0000a002-1e4b-4f6a-9c3d-2b7e5a8f0c11"

    /** 电脑 -> 手机：结果与状态（Notify） */
    val CHAR_TX_UUID: String = "0000a003-1e4b-4f6a-9c3d-2b7e5a8f0c11"

    /** 客户端特征配置描述符（标准 UUID，用于打开通知） */
    val CCCD_UUID: String = "00002902-0000-1000-8000-00805f9b34fb"

    /** 电脑端开机时广播的固定标识，手机靠它认出目标设备 */
    const val ADVERTISED_NAME: String = "PC-Shutdown"

    /**
     * 广播数据段里的自定义标识前缀（与电脑端 ADVERT_MARKER 的前 4 字节一致）。
     *
     * 电脑端实际发的是 `PSD1` + 协议版本字节（当前 0x02）。手机只匹配前 4 字节，
     * 这样协议版本升级后旧/新电脑都能被认出来，不会因为一个版本字节就失联。
     */
    val ADVERT_MARKER_PREFIX: ByteArray = byteArrayOf(
        'P'.code.toByte(), 'S'.code.toByte(), 'D'.code.toByte(), '1'.code.toByte()
    )

    /** 电脑端版本，协议不匹配时手机可以提示升级 */
    const val PROTOCOL_VERSION: Int = 2

    // ---- 命令字（手机 -> 电脑） ----

    const val CMD_HELLO = "HELLO"
    const val CMD_AUTH = "AUTH"
    const val CMD_AUTH2 = "AUTH2"

    /**
     * 协议 v2 的认证应答：HMAC-SHA256(PIN, nonce) 的小写十六进制。
     *
     * PIN 本身不上蓝牙链路，旁听者拿不到；nonce 每次认证后作废，重放无效。
     */
    fun hmacHex(pin: String, nonce: String): String {
        val mac = javax.crypto.Mac.getInstance("HmacSHA256")
        mac.init(
            javax.crypto.spec.SecretKeySpec(
                pin.toByteArray(Charsets.UTF_8), "HmacSHA256"
            )
        )
        return mac.doFinal(nonce.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
    }
    const val CMD_SHUTDOWN = "SHUTDOWN"
    const val CMD_RESTART = "RESTART"
    const val CMD_SLEEP = "SLEEP"
    const val CMD_CANCEL = "CANCEL"
    const val CMD_QUERY = "QUERY"
    const val CMD_PING = "PING"
}
