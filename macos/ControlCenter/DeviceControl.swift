import AppIntents
import Darwin
import Foundation
import SwiftUI
import WidgetKit

private enum BridgeError: LocalizedError {
    case notRunning, noResponse, rejected(String?)

    var errorDescription: String? {
        switch self {
        case .notRunning: "MiHome 没有运行，请先打开 MiHome（可以只在后台运行）。"
        case .noResponse: "MiHome 没有及时响应，请稍后再试。"
        case .rejected(let reason): reason ?? "设备暂时无法控制，请在 MiHome 查看状态。"
        }
    }
}

/// App Group 信箱，协议见 app/ui/control_center_bridge.py。
private enum Bridge {
    struct Request: Encodable {
        let operation: String
        let device: String?
        let isOn: Bool?
        let expires: Double
    }
    struct Reply: Decodable {
        let ok: Bool
        let error: String?
        let devices: [MiHomeDevice]?
        let isOn: Bool?
        let name: String?
        let room: String?
        let home: String?
        let symbol: String?
    }

    static var mailbox: URL? {
        guard let group = Bundle.main.object(forInfoDictionaryKey: "MiHomeControlGroup") as? String,
              let container = FileManager.default.containerURL(
                forSecurityApplicationGroupIdentifier: group)
        else { return nil }
        return container.appendingPathComponent("MiHomeControls", isDirectory: true)
    }

    /// MiHome 运行期间一直持有 host.lock 的排他锁；能拿到共享锁就说明它没在运行。
    static func hostIsRunning(_ directory: URL) -> Bool {
        let fd = open(directory.appendingPathComponent("host.lock").path, O_RDONLY)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        if flock(fd, LOCK_SH | LOCK_NB) == 0 {
            flock(fd, LOCK_UN)
            return false
        }
        return true
    }

    static func send(_ operation: String, device: String? = nil, isOn: Bool? = nil,
                     timeout: TimeInterval = 8) async throws -> Reply {
        guard let directory = mailbox, hostIsRunning(directory) else {
            throw BridgeError.notRunning
        }
        let fm = FileManager.default
        let key = UUID().uuidString
        let request = directory.appendingPathComponent(key + ".request")
        let reply = directory.appendingPathComponent(key + ".reply")
        let deadline = Date().addingTimeInterval(timeout)
        let message = Request(operation: operation, device: device, isOn: isOn,
                              expires: deadline.timeIntervalSince1970)
        try JSONEncoder().encode(message).write(to: request, options: .atomic)
        defer {
            try? fm.removeItem(at: request)
            try? fm.removeItem(at: reply)
        }
        while Date() < deadline {
            if let data = try? Data(contentsOf: reply) {
                let response = try JSONDecoder().decode(Reply.self, from: data)
                guard response.ok else { throw BridgeError.rejected(response.error) }
                return response
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        throw BridgeError.noResponse
    }
}

/// 上次拿到的设备列表。系统解析已配置的设备时直接用它，不必每次都联网读全部设备。
private enum DeviceCache {
    static var url: URL? { Bridge.mailbox?.appendingPathComponent("devices.json") }

    // 家庭与设备选取共用一份短期快照，避免每次展开或搜索都请求米家。
    static func recent() -> [MiHomeDevice]? {
        guard let url,
              let modified = try? url.resourceValues(forKeys: [.contentModificationDateKey])
                  .contentModificationDate,
              Date().timeIntervalSince(modified) < 30
        else { return nil }
        let devices = load()
        return devices.isEmpty ? nil : devices
    }

    static func load() -> [MiHomeDevice] {
        guard let url, let data = try? Data(contentsOf: url) else { return [] }
        return (try? JSONDecoder().decode([MiHomeDevice].self, from: data)) ?? []
    }

    static func save(_ devices: [MiHomeDevice]) {
        guard let url, let data = try? JSONEncoder().encode(devices) else { return }
        try? data.write(to: url, options: .atomic)
    }
}

struct MiHomeDevice: AppEntity, Codable {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "米家设备"
    static let defaultQuery = MiHomeDeviceQuery()
    static let placeholder = MiHomeDevice(id: "", name: "米家设备", room: "")
    let id: String
    let name: String
    let room: String
    var home: String = ""
    var online: Bool = true
    var symbol: String = "power"
    var thumbnail: Data? = nil

    var displayRepresentation: DisplayRepresentation {
        let image = thumbnail.map { DisplayRepresentation.Image(data: $0, isTemplate: false) }
            ?? DisplayRepresentation.Image(systemName: symbol)
        let subtitle = [room, online ? "" : "离线"].filter { !$0.isEmpty }.joined(separator: " · ")
        return DisplayRepresentation(title: "\(name)", subtitle: "\(subtitle)", image: image)
    }
}

extension MiHomeDevice {
    // 旧版缓存里没有 home/online，按默认值读出，不让整份缓存失效
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        name = try values.decode(String.self, forKey: .name)
        room = try values.decodeIfPresent(String.self, forKey: .room) ?? ""
        home = try values.decodeIfPresent(String.self, forKey: .home) ?? ""
        online = try values.decodeIfPresent(Bool.self, forKey: .online) ?? true
        symbol = try values.decodeIfPresent(String.self, forKey: .symbol) ?? "power"
        thumbnail = try? values.decodeIfPresent(Data.self, forKey: .thumbnail)
    }
}

/// 配置控件时先选的家庭；设备列表按它筛选。用家庭名作标识（设备列表只带家庭名）。
struct MiHomeHome: AppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "米家家庭"
    static let defaultQuery = MiHomeHomeQuery()
    let id: String

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(id)")
    }
}

struct MiHomeHomeQuery: EntityStringQuery {
    func suggestedEntities() async throws -> [MiHomeHome] {
        var seen = Set<String>()
        return try await MiHomeDeviceQuery.allDevices().compactMap {
            seen.insert($0.home).inserted ? MiHomeHome(id: $0.home) : nil
        }
    }

    func entities(matching string: String) async throws -> [MiHomeHome] {
        try await suggestedEntities().filter {
            string.isEmpty || $0.id.localizedStandardContains(string)
        }
    }

    func entities(for identifiers: [String]) async throws -> [MiHomeHome] {
        identifiers.map { MiHomeHome(id: $0) }
    }
}

struct MiHomeDeviceQuery: EntityStringQuery {
    /// 配置界面里已选的家庭；选完家庭才显示设备
    @IntentParameterDependency<ChooseDevice>(\.$home) var configuration

    static func allDevices() async throws -> [MiHomeDevice] {
        if let recent = DeviceCache.recent() { return recent }
        let cached = DeviceCache.load()
        do {
            let devices = try await Bridge.send("list", timeout: cached.isEmpty ? 8 : 0.5).devices ?? []
            DeviceCache.save(devices)
            return devices
        } catch {
            // MiHome 暂时不可用时仍可用上次的列表完成配置
            let cached = DeviceCache.load()
            if cached.isEmpty { throw error }
            return cached
        }
    }

    func suggestedEntities() async throws -> IntentItemCollection<MiHomeDevice> {
        try await entities(matching: "")
    }

    func entities(matching string: String) async throws -> IntentItemCollection<MiHomeDevice> {
        guard let home = configuration?.home else {
            return IntentItemCollection(promptLabel: "请先选择家庭", items: [])
        }
        let devices = try await Self.allDevices()
        let matches = devices.filter { device in
            device.home == home.id
                && (string.isEmpty || device.name.localizedStandardContains(string)
                    || device.room.localizedStandardContains(string))
        }
        let rooms = Dictionary(grouping: matches) { $0.room.isEmpty ? "未分配房间" : $0.room }
        let sections = rooms.keys.sorted { $0.localizedStandardCompare($1) == .orderedAscending }
            .map { room in
                IntentItemSection<MiHomeDevice>("\(room)", items: rooms[room] ?? [])
            }
        return IntentItemCollection(promptLabel: "选择设备", sections: sections)
    }

    func entities(for identifiers: [String]) async throws -> [MiHomeDevice] {
        var known = Dictionary(DeviceCache.load().map { ($0.id, $0) },
                               uniquingKeysWith: { first, _ in first })
        if identifiers.contains(where: { known[$0] == nil }),
           let fresh = try? await Bridge.send("list").devices {
            DeviceCache.save(fresh)
            for device in fresh { known[device.id] = device }
        }
        // 找不到的设备也返回占位实体，不让系统把控件的配置清空；
        // 取状态时 MiHome 会给出真实名称或「不可用」
        return identifiers.map {
            known[$0] ?? MiHomeDevice(id: $0, name: MiHomeDevice.placeholder.name, room: "")
        }
    }
}

struct ChooseDevice: ControlConfigurationIntent {
    static let title: LocalizedStringResource = "选择米家设备"
    // 配置界面按声明顺序排列：先家庭，再该家庭里的设备
    @Parameter(title: "家庭") var home: MiHomeHome?
    @Parameter(title: "设备") var device: MiHomeDevice?

    var selectedDevice: MiHomeDevice? {
        guard let device else { return nil }
        // 兼容早期只保存设备的控件；新配置的设备必须属于所选家庭。
        if let home, !device.home.isEmpty, device.home != home.id { return nil }
        return device
    }
}

struct SetDevicePower: SetValueIntent {
    static let title: LocalizedStringResource = "设置米家设备开关"
    @Parameter(title: "设备") var device: MiHomeDevice
    @Parameter(title: "开关") var value: Bool

    init() {}
    init(device: MiHomeDevice) { self.device = device }

    func perform() async throws -> some IntentResult {
        guard !device.id.isEmpty else {
            throw BridgeError.rejected("请先编辑这个控件，为它选择一台米家设备。")
        }
        _ = try await Bridge.send("set", device: device.id, isOn: value)
        return .result()
    }
}

struct DevicePowerProvider: AppIntentControlValueProvider {
    struct Value {
        let device: MiHomeDevice
        let isOn: Bool
        /// 有值时代替「开/关」显示，说明为什么现在没法控制
        let status: String?
    }

    func previewValue(configuration: ChooseDevice) -> Value {
        Value(device: configuration.selectedDevice ?? .placeholder, isOn: false, status: nil)
    }

    func currentValue(configuration: ChooseDevice) async throws -> Value {
        guard let device = configuration.selectedDevice, !device.id.isEmpty else {
            return Value(device: .placeholder, isOn: false, status: "未选择设备")
        }
        do {
            let reply = try await Bridge.send("state", device: device.id, timeout: 6)
            guard let isOn = reply.isOn else { throw BridgeError.rejected(nil) }
            let current = MiHomeDevice(id: device.id, name: reply.name ?? device.name,
                                       room: reply.room ?? device.room,
                                       home: reply.home ?? device.home,
                                       symbol: reply.symbol ?? device.symbol)
            return Value(device: current, isOn: isOn, status: nil)
        } catch BridgeError.notRunning {
            return Value(device: device, isOn: false, status: "MiHome 未运行")
        } catch BridgeError.rejected(let reason) where reason == "设备离线" {
            return Value(device: device, isOn: false, status: "离线")
        } catch {
            // MiHome 恢复后会请系统重新取值（见 control_center_bridge.py）
            return Value(device: device, isOn: false, status: "不可用")
        }
    }
}

struct DevicePowerControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        AppIntentControlConfiguration(kind: "io.github.huanyuejue.mihome.device-power",
                                      provider: DevicePowerProvider()) { value in
            ControlWidgetToggle(value.device.name, isOn: value.isOn,
                                action: SetDevicePower(device: value.device)) { isOn in
                Label(value.status ?? (isOn ? "开" : "关"), systemImage: value.device.symbol)
            }
        }
        .displayName("米家设备开关")
        .description("查看并切换一台米家设备的电源。")
        .promptsForUserConfiguration()
    }
}

@main
struct MiHomeControls: WidgetBundle {
    var body: some Widget { DevicePowerControl() }
}
