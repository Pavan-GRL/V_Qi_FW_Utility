# STM32H7 + NetX Duo UDP Transmission Rate Analysis

## Your Current FSK Data Rate

### From Wireshark Analysis:
```
- Packet size: ~1376 bytes (16 byte header + 170 records × 8 bytes)
- Records per packet: 170
- Time between packets: ~0.7-0.8 ms (when working)
- Effective packet rate: ~1,250 packets/sec
```

### Calculated Data Rate:
```
Packet size:        1,376 bytes
Packet rate:        1,250 pps
Raw data rate:      1,376 × 1,250 = 1,720,000 bytes/sec
                    = 1.64 MB/s
                    = 13.1 Mbps (at application layer)

With UDP overhead:
UDP header:         8 bytes
IP header:          20 bytes
Ethernet header:    14 bytes + 4 byte CRC
Total overhead:     46 bytes per packet

Total packet size:  1,376 + 46 = 1,422 bytes
Wire rate:          1,422 × 1,250 = 1,777,500 bytes/sec
                    = 1.69 MB/s
                    = ~14.2 Mbps on wire
```

---

## STM32H7 Network Capabilities

### Hardware Specs (STM32H7 with Ethernet PHY):

| Component | Capability |
|-----------|-----------|
| **Ethernet MAC** | 10/100/1000 Mbps |
| **CPU (STM32H7)** | Up to 480 MHz (Cortex-M7) |
| **RAM** | 1 MB SRAM, 16 MB SDRAM (external) |
| **DMA** | Yes, for Ethernet |
| **PHY Support** | 100 Mbps typical (LAN8742A, DP83848, etc.) |

**✓ Hardware can theoretically support 14.2 Mbps** (well under 100 Mbps limit)

---

## NetX Duo Stack Performance

### Typical NetX Duo Performance Metrics:

| Metric | Value | Your Requirement |
|--------|-------|------------------|
| **Max UDP throughput** | 50-80 Mbps | 14.2 Mbps ✓ |
| **Typical packet rate** | 5,000-10,000 pps | 1,250 pps ✓ |
| **TX buffer requirement** | Depends on config | Need to check |
| **Min packet interval** | ~100 µs | 700-800 µs ✓ |

**✓ NetX Duo should handle 14.2 Mbps easily**

---

## ⚠️ LIKELY BOTTLENECKS (Why You're Seeing Packet Loss)

### 1. **NetX Duo Packet Pool Exhausted**

This is the **#1 most common issue**!

```c
// Typical NetX Duo configuration:
#define NX_PACKET_POOL_SIZE         (10 * 1536)  // Only 10 packets!
#define NX_TCP_SOCKET_WINDOW        2048          // Small
```

**Problem:**
- You're sending 1,250 packets/sec
- If packet pool only has 10-20 packets
- At 0.8ms intervals, you need ~15-20 packets in flight
- **Pool exhaustion = dropped packets**

**Symptoms match yours:**
- Intermittent gaps (when pool empties)
- Regular pattern (~every 6-7ms as you saw)
- Time gaps where TX stops briefly

---

### 2. **ThreadX TX Thread Priority Too Low**

```c
// If your UDP TX thread has low priority:
tx_thread_create(&udp_tx_thread, ..., priority=15);  // Too low!

// And other threads are higher priority:
tx_thread_create(&gui_thread, ..., priority=5);      // Higher = runs first
```

**Problem:**
- Higher priority threads preempt UDP transmission
- TX thread gets starved
- Packets queue up and overflow

---

### 3. **NetX Duo ARP Cache Issues**

```c
#define NX_ARP_CACHE_SIZE    10  // Only 10 entries
```

**Problem:**
- If ARP entry expires during transmission
- NetX Duo pauses to send ARP request
- Causes the 3-7ms gaps you're seeing

---

### 4. **DMA/Buffer Configuration**

```c
// Ethernet driver buffer configuration
#define ETH_TXBUFNB    4   // Only 4 TX buffers!
#define ETH_RXBUFNB    4   // Only 4 RX buffers!
```

**Problem:**
- With only 4 TX buffers
- At high packet rates, buffers get exhausted
- HAL_ETH_Transmit() fails, packets dropped

---

### 5. **CPU Overload from Data Generation**

```c
// If you're doing this every 0.7ms:
for (int i = 0; i < 170; i++) {
    fsk_data[i] = process_fsk_sample();  // Expensive!
}
nx_udp_socket_send(...);
```

**Problem:**
- 170 samples processed every 0.7ms
- That's ~242,000 samples/second!
- STM32H7 at 480 MHz may struggle if processing is complex

---

## 🔧 RECOMMENDED FIXES FOR STM32H7 + NetX Duo

### Fix #1: Increase NetX Duo Packet Pool (CRITICAL)

```c
// In nx_user.h or your NetX config:

// OLD (insufficient):
#define NX_PACKET_POOL_SIZE    (10 * 1536)

// NEW (sufficient for your rate):
#define NX_PACKET_POOL_SIZE    (100 * 1536)  // 100 packets minimum
                                              // ~150 KB RAM usage

// Or calculate dynamically:
// Packets in flight = (rate × latency) = 1250 pps × 0.020s = 25 packets
// Add 4x safety margin = 100 packets
```

**Expected result:** Eliminates most packet loss

---

### Fix #2: Increase Ethernet DMA Buffers

```c
// In stm32h7xx_hal_conf.h or eth driver:

// OLD:
#define ETH_TXBUFNB    4   // Insufficient
#define ETH_RXBUFNB    4

// NEW:
#define ETH_TXBUFNB    16  // 4x increase
#define ETH_RXBUFNB    8   // RX less critical for TX-heavy app

// Memory cost: (16 + 8) × 1536 = ~37 KB
```

---

### Fix #3: Increase ThreadX TX Thread Priority

```c
// Your UDP TX thread should be HIGH priority:
tx_thread_create(&fsk_udp_tx_thread,
                 "FSK TX",
                 fsk_tx_thread_entry,
                 0,
                 stack_ptr,
                 4096,
                 5,          // Priority 5 (high, lower number = higher priority)
                 5,
                 TX_NO_TIME_SLICE,
                 TX_AUTO_START);
```

**Note:** Priority 0-10 are highest in ThreadX (lower number = higher priority)

---

### Fix #4: Optimize ARP Settings

```c
// Increase ARP cache:
#define NX_ARP_CACHE_SIZE              64

// Increase ARP entry lifetime:
#define NX_ARP_EXPIRATION_RATE         (10 * NX_IP_PERIODIC_RATE)  // 10 seconds

// Disable ARP for unicast to known host (if talking to single receiver):
nx_arp_static_entry_create(&ip_instance,
                           RECEIVER_IP,
                           RECEIVER_MAC_ADDR);
```

---

### Fix #5: Reduce Records Per Packet

Instead of 170 records/packet, try **50-85 records/packet**:

```c
// OLD (causes buffer pressure):
#define FSK_RECORDS_PER_PACKET    170

// NEW (gentler on buffers):
#define FSK_RECORDS_PER_PACKET    50

// Packet size: 16 + (50 × 8) = 416 bytes (much smaller)
// Still fast: 1250 pps × 50 records = 62,500 records/sec
```

**Benefits:**
- Smaller packets = faster TX
- Less memory pressure
- More responsive system
- Easier to fit in packet pool

---

### Fix #6: Add Packet Pool Monitoring

Add diagnostics to detect exhaustion:

```c
void fsk_tx_thread_entry(ULONG thread_input) {
    NX_PACKET *packet;
    ULONG available, missing;

    while (1) {
        // Check pool health
        nx_packet_pool_info_get(&packet_pool,
                                NULL,
                                NULL,
                                &available,
                                &missing,
                                NULL,
                                NULL);

        if (available < 10) {
            printf("WARNING: Packet pool low! Available=%lu\n", available);
        }

        if (missing > 0) {
            printf("ERROR: Packet allocation failures! Missing=%lu\n", missing);
        }

        // Allocate and send packet
        status = nx_packet_allocate(&packet_pool, &packet,
                                    NX_UDP_PACKET,
                                    NX_WAIT_FOREVER);  // Or use timeout

        if (status != NX_SUCCESS) {
            printf("ERROR: Packet alloc failed! Status=0x%x\n", status);
            continue;
        }

        // Fill and send packet...
        nx_udp_socket_send(&fsk_socket, packet,
                          receiver_ip, receiver_port);

        tx_thread_sleep(1);  // 10ms at default tick rate (adjust as needed)
    }
}
```

---

### Fix #7: Use Zero-Copy Where Possible

```c
// AVOID copying data unnecessarily:

// BAD (double copy):
uint8_t temp_buffer[1400];
fill_fsk_data(temp_buffer, 170);
nx_packet_data_append(packet, temp_buffer, 1376, &packet_pool, NX_WAIT_FOREVER);

// GOOD (direct write):
UCHAR *data_ptr = packet->nx_packet_prepend_ptr;
fill_fsk_data_direct(data_ptr, 170);  // Write directly to packet buffer
packet->nx_packet_append_ptr = packet->nx_packet_prepend_ptr + 1376;
packet->nx_packet_length = 1376;
```

---

## 📊 Bandwidth Utilization Analysis

### Current Configuration (170 records/packet):
```
Application:   13.1 Mbps
Wire:          14.2 Mbps
100M Ethernet: 14.2%  utilization ✓ (plenty of headroom)
```

### With Optimizations (50 records/packet):
```
Packet rate:   3,125 pps (2.5x more packets, but smaller)
Application:   Still 13.1 Mbps (same data)
Wire:          15.5 Mbps (more UDP/IP overhead)
100M Ethernet: 15.5%  utilization ✓ (still fine)
```

**Both configurations are well within limits!**

---

## 🎯 PRIORITY ACTION ITEMS

### Immediate (Do These First):

1. **Increase packet pool**: 10 → 100 packets
2. **Increase DMA buffers**: ETH_TXBUFNB 4 → 16
3. **Add packet pool monitoring**: Detect exhaustion
4. **Reduce records/packet**: 170 → 50 or 85

### Secondary (If issues persist):

5. **Check TX thread priority**: Should be 5-10
6. **Add static ARP entry**: Eliminate ARP delays
7. **Profile CPU usage**: Ensure <80% during transmission

### Testing (Verify the fixes):

8. **Monitor packet pool stats**: `nx_packet_pool_info_get()`
9. **Check for allocation failures**: Log missing packets
10. **Re-capture with Wireshark**: Verify gaps eliminated

---

## 📝 CONFIGURATION CHECKLIST

```c
/* ===== NetX Duo Configuration ===== */
#define NX_PACKET_POOL_SIZE            (100 * 1536)  // ✓ 100 packets
#define NX_ARP_CACHE_SIZE              64            // ✓ Increased
#define NX_ARP_EXPIRATION_RATE         (10 * NX_IP_PERIODIC_RATE)

/* ===== Ethernet Driver Configuration ===== */
#define ETH_TXBUFNB                    16            // ✓ Increased
#define ETH_RXBUFNB                    8             // ✓ Sufficient

/* ===== FSK Application Configuration ===== */
#define FSK_RECORDS_PER_PACKET         50            // ✓ Reduced
#define FSK_TX_THREAD_PRIORITY         5             // ✓ High priority
#define FSK_TX_THREAD_STACK_SIZE       4096          // ✓ Sufficient

/* ===== ThreadX Configuration ===== */
#define TX_TIMER_TICKS_PER_SECOND      100           // Default OK
```

---

## 🔍 HOW TO VERIFY ROOT CAUSE

Add this debug code to your STM32 FSK sender:

```c
void fsk_tx_debug_stats(void) {
    ULONG total, free, empty_pool_requests, empty_pool_suspensions;
    ULONG invalid_releases, failed_sends;

    nx_packet_pool_info_get(&packet_pool,
                            &total,
                            &free,
                            &empty_pool_requests,
                            &empty_pool_suspensions,
                            &invalid_releases,
                            NULL);

    printf("\n=== NetX Duo Packet Pool Stats ===\n");
    printf("Total packets:        %lu\n", total);
    printf("Free packets:         %lu\n", free);
    printf("Empty pool requests:  %lu  %s\n",
           empty_pool_requests,
           empty_pool_requests > 0 ? "<-- PROBLEM!" : "OK");
    printf("Pool suspensions:     %lu  %s\n",
           empty_pool_suspensions,
           empty_pool_suspensions > 0 ? "<-- PROBLEM!" : "OK");
    printf("Invalid releases:     %lu\n", invalid_releases);

    // If empty_pool_requests > 0, you found the problem!
}
```

Run this every few seconds during transmission. If `empty_pool_requests` is non-zero, **that's your smoking gun!**

---

## ✅ EXPECTED RESULTS AFTER FIXES

- **Zero packet loss** in Wireshark captures
- **Continuous sequence numbers** in CSV (no gaps)
- **Packet pool never exhausted** (empty_pool_requests = 0)
- **Consistent ~0.7-0.8ms timing** between packets
- **No large time gaps** (3-7ms delays eliminated)

---

## 📚 Additional Resources

- **NetX Duo User Guide**: Chapter 3 (Packet Pools)
- **STM32H7 Ethernet**: AN4838 (Ethernet tips)
- **ThreadX User Guide**: Chapter 5 (Thread Priorities)

Need the actual STM32 code to review? Share your NetX Duo initialization and FSK transmission code, and I can provide specific line-by-line fixes!
