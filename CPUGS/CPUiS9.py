#物流改为事件

import order_generate5
import LLF_LS6
import JT_MS
import PA_SS2
import heapq
import time
import itertools
import random
from copy import deepcopy
import Parameter_settings as ps

# 性能控制：测试时由 test3.py 设为 False，跳过所有调试输出
VERBOSE = True

# 长程调度参数
normal_released_capacity = ps.normal_released_capacity  # 无紧急订单时维持的released订单数量
max_released_order_capacity = ps.max_released_order_capacity

# ==================== 全局变量：车间资源 ====================
TOTAL_WORKSTATIONS = ps.TOTAL_WORKSTATIONS  # 总工作站数量
# 物流时间定义
MEMORY_TO_CACHE_TIME = ps.MEMORY_TO_CACHE_TIME  # 从memory运送到cache的时间
HARDDISK_TO_MEMORY_TIME = ps.HARDDISK_TO_MEMORY_TIME  # 从harddisk运送到memory的时间
WIP_TRANSFER_TIME = ps.WIP_TRANSFER_TIME  # 工作站之间WIP运输时间
LOAD_TIME = ps.LOAD_TIME
# 材料类型定义
MATERIAL_TYPES = list(range(1, 11))  # 材料类型：1-10
# 内存区域：存放3D打印物料（每种材料的数量）
MEMORY_CAPACITY = ps.MEMORY_CAPACITY  # 总容量限制
memory_storage = {material_type: 1 for material_type in MATERIAL_TYPES}  # 初始化每种材料1个
#订单信息
import order_generate5
order_file_name="orders_100.json"
# order_file_name="new_orders.json"
data = order_generate5.read_orders(order_file_name)
orders = data["orders"]
EU_events = data["EU_events"]
# print(orders)
# print(EU_events)

# 全局仿真时钟
current_time = 0

# 统计指标
total_setup_time = 0  # 总准备时间（材料切换时间）
job_start_times = {}  # 记录每个job的开始时间 {job_id: start_time}
job_completion_times = {}  # 记录每个job的完成时间 {job_id: completion_time}
order_completion_times = {}  # 记录每个order的完成时间 {order_id: completion_time}
order_release_times = {}  # 记录每个order的release时间 {order_id: release_time}

random.seed(42)

def _transport_factor():
    """运输时间随机倍率 ~ U[1-0.2ε, 1+0.2ε]，ε=0时恒为1（无波动）"""
    eps = ps.uncertainty_intensity
    return random.uniform(1 - 0.2 * eps, 1 + 0.2 * eps)

# region 事件类定义
_counter = itertools.count()
class Event:
    """事件基类"""

    def __init__(self, event_type, time, prior=0, **kwargs):
        self.event_type = event_type
        self.time = time
        self.prior = prior  # 0 = 普通，-1 = 插队
        self.counter = next(_counter)  # 全局递增计数
        self.data = kwargs

    def __lt__(self, other):
        return (self.time, self.prior, self.counter) < \
               (other.time, other.prior, other.counter)

    def __repr__(self):
        return f"Event(type={self.event_type}, time={self.time}, priority={self.prior}, data={self.data})"

    # def __str__(self):
    #     if not self.queue:
    #         return "事件队列：空"
    #     return "事件队列：[" + ", ".join(str(event) for event in self.queue) + "]"


class EventQueue:
    """事件队列管理"""

    def __init__(self):
        self.queue = []

    def add_event(self, event):
        """普通添加"""
        event.prior = 0
        heapq.heappush(self.queue, event)

    def add_event_front(self, event):
        """同一时间插队：优先级更高"""
        event.prior = -1
        heapq.heappush(self.queue, event)

    def get_next_event(self):
        """获取下一个事件"""
        if self.queue:
            return heapq.heappop(self.queue)
        return None

    def is_empty(self):
        """检查队列是否为空"""
        return len(self.queue) == 0

    def peek_next_time(self):
        """查看下一个事件的时间（不移除）"""
        if self.queue:
            return self.queue[0].time
        return float('inf')


# 全局事件队列
event_queue = EventQueue()


# endregion

# region 资源类
#局部ready队列中job的优先级定义
def get_job_priority(job, orders, current_time):
    """
    获取job的优先级（基于其所属order的laxity）
    :param job: job对象
    :param orders: 全局orders列表
    :param current_time: 当前仿真时间
    :return: laxity值（越小优先级越高）
    """
    # 从job_id中提取order_id
    job_id = job["job_id"]
    order_id = int(job_id.split('_')[0])

    # 在全局orders中查找对应的order
    target_order = None
    for order in orders:
        if order["order_id"] == order_id:
            target_order = order
            break

    if target_order is None:
        # 如果找不到order，返回一个很大的值（最低优先级）
        return float('inf')

    # 计算order的laxity
    laxity = LLF_LS6.calculate_order_laxity(target_order, current_time)
    return laxity

# ==================== 工作站类定义 ====================
class WorkStation:
    def __init__(self, ws_id, printer_type):
        self.ws_id = ws_id  # 工作站ID
        self.job_queue = []  # 局部队列：记录分配到该工作站的作业
        # 资源数量（合并后的资源）
        self.id_resource = 1  # ID资源（id_designer + scanner_3d）
        self.md_resource = 1  # MD资源（md_designer + computer）
        self.printer_3d_type = printer_type  # 3D打印机类型
        self.printer_3d = 1  # 3D打印机数量
        self.printer_material = 0  # 当前load的material
        self.logistics_for_stealing = 0  # 是否需要进行物流交换
        self.assembly_resource = 1  # 装配资源（assembly_station + assembly_worker）
        self.cache_area = {material_type: 0 for material_type in MATERIAL_TYPES}
        self.cache_capacity = 3  # Cache总容量
        self.temp_wip_storage = []  # 临时WIP存储区（Work In Progress）
        # 资源占用状态（追踪哪些资源正在被使用）
        self.id_resource_busy = 0  # 忙碌的ID资源数量
        self.md_resource_busy = 0
        self.printer_3d_busy = 0
        self.assembly_resource_busy = 0
        # 繁忙时间统计（workload balance）
        self.busy_time = {
            "id_resource": 0.0,
            "md_resource": 0.0,
            "printer_3d": 0.0,
            "assembly_resource": 0.0
        }
        self.busy_start = {
            "id_resource": None,
            "md_resource": None,
            "printer_3d": None,
            "assembly_resource": None
        }

    # region 局部队列相关定义与操作
    def sort_job_queues(self, current_time):
        """
        对工作站的job队列按照所属order的laxity进行排序
        laxity越小，优先级越高（越紧急）
        """
        # 按laxity升序排序（laxity越小越优先）
        self.job_queue.sort(key=lambda job: get_job_priority(job, orders, current_time))

    def add_job(self, job, current_time):
        """
        将job添加到工作站队列并重新排序
        :param job: 完整的job对象
        :param current_time: 当前仿真时间
        """
        self.job_queue.append(job)
        self.sort_job_queues(current_time)

    def pop_job(self, job):
        """
        从队列中移除指定的job
        :param job: 要移除的job对象
        :return: 被移除的job对象，如果job不在队列中则返回None
        """
        if job in self.job_queue:
            self.job_queue.remove(job)
            return job
        else:
            return None
    # endregion

    def add_material_to_cache(self, material_type, quantity=1):
        """向cache区添加物料"""
        if sum(self.cache_area.values()) + quantity <= self.cache_capacity:
            self.cache_area[material_type] += quantity
            print(f"工作站 {self.ws_id}: 向Cache添加 {quantity} 个类型 {material_type} 的物料")
            return True
        else:
            print(f"工作站 {self.ws_id}: Cache区已满，无法添加物料")
            return False

    def remove_material_from_cache(self, material_type, quantity=1):
        """从cache区取出物料"""
        if self.cache_area[material_type] >= quantity:
            self.cache_area[material_type] -= quantity
            print(f"工作站 {self.ws_id}: 从Cache取出 {quantity} 个类型 {material_type} 的物料")
            return True
        else:
            print(f"工作站 {self.ws_id}: Cache区类型 {material_type} 的物料不足")
            return False

    def load_material_from_cache(self, material_type):
        """从cache区加载物料到机器中，不影响cache中的物料数量"""
        global total_setup_time
        if self.printer_material == material_type:
            return 0
        else:
            if self.cache_area[material_type] > 0:
                self.printer_material = material_type
                print(f"工作站 {self.ws_id}: 从Cache加载类型 {material_type} 的物料到3D打印机")
                total_setup_time += LOAD_TIME  # 记录setup time
                return LOAD_TIME
            else:
                print(f"工作站 {self.ws_id}: Cache区类型 {material_type} 的物料不足")

    def transfer_wip_to(self, target_workstation, wip):
        """将WIP转移到另一个工作站（需要花费时间）"""
        if wip in self.temp_wip_storage:
            self.temp_wip_storage.remove(wip)
            target_workstation.temp_wip_storage.append(wip)
            print(f"WIP转移: {self.ws_id} -> {target_workstation.ws_id}, "
                  f"WIP: {wip}, 耗时: {WIP_TRANSFER_TIME}")
            return WIP_TRANSFER_TIME
        else:
            print(f"工作站 {self.ws_id}: WIP {wip} 不存在于存储区")
            return 0

    def check_resource_available(self, operation, job=None):
        """
        检查执行某个操作所需的资源是否可用
        :param operation: 操作对象
        :param job: 操作所属的job（用于A操作的WIP检查）
        :return: True表示资源可用，False表示资源不足
        """
        o_type = operation["o_type"]
        if o_type == "D":
            if operation.get("ID_started", False) and not operation.get("ID_completed", False):
                return False
            elif not operation.get("ID_started", False):
                return self.id_resource - self.id_resource_busy > 0
            elif operation.get("ID_completed", False) and not operation.get("MD_completed", False):
                return self.md_resource - self.md_resource_busy > 0
            else:
                return False
        elif o_type == "P":
            material_needed = operation["material"]
            return (self.printer_3d - self.printer_3d_busy > 0 and
                    self.cache_area[material_needed] > 0)
        elif o_type == "A":
            # 检查assembly资源
            if not (self.assembly_resource - self.assembly_resource_busy > 0):
                return False
            # 检查WIP是否到位（A操作需要本job的P操作的WIP在本ws）
            if job is not None:
                for op in job["operations"]:
                    if op["o_type"] == "P":
                        if op["o_id"] not in self.temp_wip_storage:
                            return False
                        break
            return True
        return False

    def occupy_resource(self, operation):
        """
        占用执行操作所需的资源
        :param operation: 操作对象
        :return: 执行时间
        """
        o_type = operation["o_type"]
        exec_time = 0
        if o_type == "D":
            if not operation.get("ID_started", False):
                self.id_resource_busy += 1
                self.record_busy_start("id_resource")  # 记录繁忙开始
                operation["ID_started"] = True
                exec_time = operation["ID_time"]
                print(f"  占用资源: ID资源 | 执行时间: {exec_time}")
            elif operation.get("ID_completed", False) and not operation.get("MD_completed", False):
                self.md_resource_busy += 1
                self.record_busy_start("md_resource")  # 记录繁忙开始
                operation["MD_started"] = True
                exec_time = operation["MD_time"]
                print(f"  占用资源: MD资源 | 执行时间: {exec_time}")
        elif o_type == "P":
            self.printer_3d_busy += 1
            self.record_busy_start("printer_3d")  # 记录繁忙开始
            material_needed = operation["material"]
            load_time = self.load_material_from_cache(material_needed)
            machine_times = operation["machine_times"]
            exec_time = machine_times[str(self.printer_3d_type)]["actual_time"] + load_time
            print(f"  占用资源: 3D打印机(类型{self.printer_3d_type}) | 执行时间: {exec_time}")
        elif o_type == "A":
            self.assembly_resource_busy += 1
            self.record_busy_start("assembly_resource")  # 记录繁忙开始
            exec_time = operation["A_time"]
            print(f"  占用资源: 装配资源 | 执行时间: {exec_time}")
        return exec_time

    def release_resource(self, operation):
        """
        释放操作占用的资源
        :param operation: 操作对象
        """
        o_type = operation["o_type"]
        if o_type == "D":
            if operation.get("MD_completed", False):
                self.accumulate_busy_time("md_resource")  # 累加繁忙时间
                self.md_resource_busy -= 1
                print(f"  释放资源: MD资源")
            elif operation.get("ID_completed", False):
                self.accumulate_busy_time("id_resource")  # 累加繁忙时间
                self.id_resource_busy -= 1
                print(f"  释放资源: ID资源")
        elif o_type == "P":
            self.accumulate_busy_time("printer_3d")  # 累加繁忙时间
            self.printer_3d_busy -= 1
            print(f"  释放资源: 3D打印机(类型{self.printer_3d_type})")
        elif o_type == "A":
            self.accumulate_busy_time("assembly_resource")  # 累加繁忙时间
            self.assembly_resource_busy -= 1
            print(f"  释放资源: 装配资源")

    def record_busy_start(self, resource_type):
        """记录资源开始繁忙的时刻（仅正常执行时调用，故障占用不调用）"""
        self.busy_start[resource_type] = current_time

    def accumulate_busy_time(self, resource_type):
        """
        累加从busy_start到当前的繁忙时间，并清除busy_start
        若busy_start为None（故障占用或已累加过），则不累加
        """
        if self.busy_start[resource_type] is not None:
            self.busy_time[resource_type] += current_time - self.busy_start[resource_type]
            self.busy_start[resource_type] = None

    def display_status(self):
        """显示工作站状态"""
        print(f"\n{'=' * 60}")
        print(f"工作站 ID: {self.ws_id}")
        print(f"{'=' * 60}")
        print(f"作业队列: {[j['job_id'] for j in self.job_queue]} (共 {len(self.job_queue)} 个作业)")
        print(f"资源状态:")
        print(f"  ID资源: {self.id_resource_busy}/{self.id_resource} (忙碌/总数)")
        print(f"  MD资源: {self.md_resource_busy}/{self.md_resource}")
        print(f"  3D打印机(类型{self.printer_3d_type}): {self.printer_3d_busy}/{self.printer_3d}")
        print(f"  装配资源: {self.assembly_resource_busy}/{self.assembly_resource}")
        print(f"Cache区物料 (总量: {sum(self.cache_area.values())}/{self.cache_capacity}):")
        print(self.cache_area)
        for mat_type, count in self.cache_area.items():
            if count > 0:
                print(f"  材料类型 {mat_type}: {count} 个")
        print(f"临时WIP存储: {self.temp_wip_storage} (共 {len(self.temp_wip_storage)} 个)")


# ==================== 内存管理函数 ====================
def add_material_to_memory(material_type, quantity=1):
    """向内存区添加物料"""
    if sum(memory_storage.values()) + quantity <= MEMORY_CAPACITY:
        memory_storage[material_type] += quantity
        print(f"已将 {quantity} 个类型 {material_type} 的物料添加到内存区")
        return True
    else:
        print("内存区已满，无法添加物料")
        return False


def remove_material_from_memory(material_type, quantity=1):
    """向内存区添加物料"""
    if memory_storage[material_type] >= quantity:
        memory_storage[material_type] -= quantity
        print(f"已将 {quantity} 个类型 {material_type} 的物料从内存区移出")
        return True
    else:
        print("内存区该类型物料不足，无法移出")
        return False


def handle_logistics_start(event):
    """
    处理物流开始事件（执行减法运算）
    :param event: 物流开始事件对象
    """
    logistics_type = event.data["logistics_type"]
    print(f"\n物流开始: {logistics_type} @ 时间 {current_time}")
    if logistics_type == "memory_cache_transfer":
        # memory -> cache 单向转移
        material_type = event.data["material_type"]
        quantity = event.data["quantity"]
        memory_storage[material_type] -= quantity
        print(f"  Memory材料{material_type} -{quantity}")
    elif logistics_type == "harddisk_cache_transfer":
        # harddisk -> memory 单向转移
        # harddisk容量无限，不需要减法操作
        print(f"  从Harddisk取出材料（容量无限）")
    elif logistics_type == "memory_cache_exchange":
        # memory ↔ cache 交换
        ws = event.data["workstation"]
        material_from_memory = event.data["material_from_memory"]
        material_from_cache = event.data["material_from_cache"]
        quantity = event.data["quantity"]
        memory_storage[material_from_memory] -= quantity
        ws.cache_area[material_from_cache] -= quantity
        print(f"  Memory材料{material_from_memory} -{quantity}, {ws.ws_id} Cache材料{material_from_cache} -{quantity}")
    elif logistics_type == "cache_harddisk_exchange":
        # memory ↔ harddisk 交换
        ws = event.data["workstation"]
        material_from_cache = event.data["material_from_cache"]
        quantity = event.data["quantity"]
        ws.cache_area[material_from_cache] -= quantity
        print(f"  Cache材料{material_from_cache} -{quantity}, 送往Harddisk")
    elif logistics_type == "steal_memory_cache_exchange":
        # memory ↔ cache 交换
        ws = event.data["workstation"]
        material_from_memory = event.data["material_from_memory"]
        material_from_cache = event.data["material_from_cache"]
        quantity = event.data["quantity"]
        memory_storage[material_from_memory] -= quantity
        ws.cache_area[material_from_cache] -= quantity
        print(f"  Memory材料{material_from_memory} -{quantity}, {ws.ws_id} Cache材料{material_from_cache} -{quantity}")
    elif logistics_type == "steal_cache_harddisk_exchange":
        # memory ↔ harddisk 交换
        ws = event.data["workstation"]
        material_from_cache = event.data["material_from_cache"]
        quantity = event.data["quantity"]
        ws.cache_area[material_from_cache] -= quantity
        print(f"  Cache材料{material_from_cache} -{quantity}, 送往Harddisk")
    elif logistics_type == "wip_transfer":
        # WIP在工作站间转移
        source_ws = event.data["source_workstation"]
        wip = event.data["wip"]
        source_ws.temp_wip_storage.remove(wip)
        print(f"  {source_ws.ws_id} WIP存储区移出 {wip}")


def handle_logistics_end(event):
    """
    处理物流结束事件（执行加法运算）
    :param event: 物流结束事件对象
    """
    logistics_type = event.data["logistics_type"]

    print(f"\n物流结束: {logistics_type} @ 时间 {current_time}")

    if logistics_type == "memory_cache_transfer":
        # memory -> cache 单向转移
        ws = event.data["workstation"]
        material_type = event.data["material_type"]
        quantity = event.data["quantity"]
        ws.cache_area[material_type] += quantity
        print(f"  {ws.ws_id} Cache材料{material_type} +{quantity}")
    elif logistics_type == "harddisk_cache_transfer":
        # harddisk -> memory 单向转移
        ws = event.data["workstation"]
        material_type = event.data["material_type"]
        quantity = event.data["quantity"]
        ws.cache_area[material_type] += quantity
        print(f"  Memory材料{material_type} +{quantity}")
    elif logistics_type == "memory_cache_exchange":
        # memory ↔ cache 交换
        ws = event.data["workstation"]
        material_from_memory = event.data["material_from_memory"]
        material_from_cache = event.data["material_from_cache"]
        quantity = event.data["quantity"]
        ws.cache_area[material_from_memory] += quantity
        memory_storage[material_from_cache] += quantity
        print(f"  {ws.ws_id} Cache材料{material_from_memory} +{quantity}, Memory材料{material_from_cache} +{quantity}")
    elif logistics_type == "cache_harddisk_exchange":
        # memory ↔ harddisk 交换
        ws = event.data["workstation"]
        material_from_harddisk = event.data["material_from_harddisk"]
        quantity = event.data["quantity"]
        ws.cache_area[material_from_harddisk] += quantity
        print(f"  Memory材料{material_from_harddisk} +{quantity}")
    elif logistics_type == "steal_memory_cache_exchange":
        # memory ↔ cache 交换
        ws = event.data["workstation"]
        material_from_memory = event.data["material_from_memory"]
        material_from_cache = event.data["material_from_cache"]
        quantity = event.data["quantity"]
        ws.cache_area[material_from_memory] += quantity
        memory_storage[material_from_cache] += quantity
        ws.logistics_for_stealing = 0
        print(f"  {ws.ws_id} Cache材料{material_from_memory} +{quantity}, Memory材料{material_from_cache} +{quantity}")
    elif logistics_type == "steal_cache_harddisk_exchange":
        # memory ↔ harddisk 交换
        ws = event.data["workstation"]
        material_from_harddisk = event.data["material_from_harddisk"]
        quantity = event.data["quantity"]
        ws.cache_area[material_from_harddisk] += quantity
        ws.logistics_for_stealing = 0
        print(f"  Memory材料{material_from_harddisk} +{quantity}")
    elif logistics_type == "wip_transfer":
        # WIP在工作站间转移
        ws = event.data["target_workstation"]
        wip = event.data["wip"]
        ws.temp_wip_storage.append(wip)
        print(f"  {ws.ws_id} WIP存储区加入 {wip}")
    elif logistics_type == "harddisk_memory_transfer":
        material_type = event.data["material_type"]
        quantity = event.data["quantity"]
        memory_storage[material_type] += quantity
        print(f"HD向MEM运输材料{material_type} +{quantity}个")
    modified_ws_ids = SS()
    if not logistics_type == "harddisk_memory_transfer":
        if ws.ws_id not in modified_ws_ids:
            create_OOOE_event(ws)



def create_logistics_event_memory_cache_transfer(workstation, material_type, quantity=1):
    """
    创建 memory -> cache 单向转移物流事件（含触发的 hd->memory 补货，二者共用一个运输倍率）
    :return: 物流耗时（memory->cache 段）
    """
    r = _transport_factor()
    dur_mc = round(MEMORY_TO_CACHE_TIME * r, 2)
    dur_hm = round(HARDDISK_TO_MEMORY_TIME * r, 2)

    # 创建物流开始事件
    start_event = Event(
        event_type="logistics_start",
        time=current_time,
        logistics_type="memory_cache_transfer",
        material_type=material_type,
        quantity=quantity
    )
    event_queue.add_event(start_event)

    # 创建物流结束事件（memory->cache）
    end_event = Event(
        event_type="logistics_end",
        time=current_time + dur_mc,
        logistics_type="memory_cache_transfer",
        workstation=workstation,
        material_type=material_type,
        quantity=quantity
    )
    event_queue.add_event(end_event)

    # 创建HD->MEM补货事件（与上面共用倍率r）
    mem_event = Event(
        event_type="logistics_end",
        time=current_time + dur_hm,
        logistics_type="harddisk_memory_transfer",
        workstation=workstation,
        material_type=material_type,
        quantity=quantity
    )
    event_queue.add_event(mem_event)

    print(f"创建物流事件: Memory -> {workstation.ws_id} Cache, 材料{material_type}, 耗时{dur_mc}")
    return dur_mc


def create_logistics_event_harddisk_cache_transfer(workstation, material_type, quantity=1):
    """
    创建 harddisk -> cache 转移物流事件（连续运输，整体一个倍率）
    :return: 物流耗时
    """
    r = _transport_factor()
    dur = round((HARDDISK_TO_MEMORY_TIME + MEMORY_TO_CACHE_TIME) * r, 2)

    start_event = Event(
        event_type="logistics_start",
        time=current_time,
        logistics_type="harddisk_cache_transfer",
        workstation=workstation,
        material_type=material_type,
        quantity=quantity
    )
    event_queue.add_event(start_event)

    end_event = Event(
        event_type="logistics_end",
        time=current_time + dur,
        workstation=workstation,
        logistics_type="harddisk_cache_transfer",
        material_type=material_type,
        quantity=quantity
    )
    event_queue.add_event(end_event)

    print(f"创建物流事件: Harddisk -> Cache, 材料{material_type}, 耗时{dur}")
    return dur


def create_logistics_event_memory_cache_exchange(workstation, material_from_memory, material_from_cache, quantity=1):
    """创建 memory ↔ cache 交换物流事件"""
    r = _transport_factor()
    dur = round(MEMORY_TO_CACHE_TIME * r, 2)

    start_event = Event(
        event_type="logistics_start",
        time=current_time,
        logistics_type="memory_cache_exchange",
        workstation=workstation,
        material_from_memory=material_from_memory,
        material_from_cache=material_from_cache,
        quantity=quantity
    )
    event_queue.add_event(start_event)

    end_event = Event(
        event_type="logistics_end",
        time=current_time + dur,
        logistics_type="memory_cache_exchange",
        workstation=workstation,
        material_from_memory=material_from_memory,
        material_from_cache=material_from_cache,
        quantity=quantity
    )
    event_queue.add_event(end_event)

    print(f"创建物流事件: Memory ↔ {workstation.ws_id} Cache交换, Memory出{material_from_memory}/入{material_from_cache}, 耗时{dur}")
    return dur


def create_logistics_event_cache_harddisk_exchange(workstation, material_from_cache, material_from_harddisk, quantity=1):
    """创建 cache ↔ harddisk 交换物流事件"""
    r = _transport_factor()
    dur = round((HARDDISK_TO_MEMORY_TIME + MEMORY_TO_CACHE_TIME) * r, 2)

    start_event = Event(
        event_type="logistics_start",
        time=current_time,
        logistics_type="cache_harddisk_exchange",
        workstation=workstation,
        material_from_cache=material_from_cache,
        material_from_harddisk=material_from_harddisk,
        quantity=quantity
    )
    event_queue.add_event(start_event)

    end_event = Event(
        event_type="logistics_end",
        time=current_time + dur,
        logistics_type="cache_harddisk_exchange",
        workstation=workstation,
        material_from_cache=material_from_cache,
        material_from_harddisk=material_from_harddisk,
        quantity=quantity
    )
    event_queue.add_event(end_event)

    print(f"创建物流事件: Cache ↔ Harddisk交换, Cache出{material_from_cache}/入{material_from_harddisk}, 耗时{dur}")
    return dur


def create_stealing_logistics_event_memory_cache_exchange(workstation, material_from_memory, material_from_cache, quantity=1):
    """创建窃取用 memory ↔ cache 交换物流事件"""
    r = _transport_factor()
    dur = round(MEMORY_TO_CACHE_TIME * r, 2)

    start_event = Event(
        event_type="logistics_start",
        time=current_time,
        logistics_type="steal_memory_cache_exchange",
        workstation=workstation,
        material_from_memory=material_from_memory,
        material_from_cache=material_from_cache,
        quantity=quantity
    )
    event_queue.add_event_front(start_event)

    end_event = Event(
        event_type="logistics_end",
        time=current_time + dur,
        logistics_type="steal_memory_cache_exchange",
        workstation=workstation,
        material_from_memory=material_from_memory,
        material_from_cache=material_from_cache,
        quantity=quantity
    )
    event_queue.add_event_front(end_event)

    print(f"创建物流事件: Memory ↔ {workstation.ws_id} Cache交换, Memory出{material_from_memory}/入{material_from_cache}, 耗时{dur}")
    return dur


def create_stealing_logistics_event_cache_harddisk_exchange(workstation, material_from_harddisk, material_from_cache, quantity=1):
    """创建窃取用 cache ↔ harddisk 交换物流事件"""
    r = _transport_factor()
    dur = round((HARDDISK_TO_MEMORY_TIME + MEMORY_TO_CACHE_TIME) * r, 2)

    start_event = Event(
        event_type="logistics_start",
        time=current_time,
        logistics_type="steal_cache_harddisk_exchange",
        workstation=workstation,
        material_from_cache=material_from_cache,
        material_from_harddisk=material_from_harddisk,
        quantity=quantity
    )
    event_queue.add_event_front(start_event)

    end_event = Event(
        event_type="logistics_end",
        time=current_time + dur,
        logistics_type="steal_cache_harddisk_exchange",
        workstation=workstation,
        material_from_cache=material_from_cache,
        material_from_harddisk=material_from_harddisk,
        quantity=quantity
    )
    event_queue.add_event_front(end_event)

    print(f"创建物流事件: Cache ↔ Harddisk交换, Cache出{material_from_cache}/入{material_from_harddisk}, 耗时{dur}")
    return dur


def create_logistics_event_wip_transfer(source_workstation, target_workstation, wip):
    """创建 WIP工作站间转移物流事件"""
    r = _transport_factor()
    dur = round(WIP_TRANSFER_TIME * r, 2)

    start_event = Event(
        event_type="logistics_start",
        time=current_time,
        logistics_type="wip_transfer",
        source_workstation=source_workstation,
        target_workstation=target_workstation,
        wip=wip
    )
    event_queue.add_event(start_event)

    end_event = Event(
        event_type="logistics_end",
        time=current_time + dur,
        logistics_type="wip_transfer",
        source_workstation=source_workstation,
        target_workstation=target_workstation,
        wip=wip
    )
    event_queue.add_event(end_event)

    print(f"创建物流事件: WIP转移 {source_workstation.ws_id} -> {target_workstation.ws_id}, WIP: {wip}, 耗时{dur}")
    return dur


def display_memory_status():
    """显示内存区状态"""
    print(f"\n{'=' * 60}")
    print(f"内存区状态 (Memory)")
    print(f"{'=' * 60}")
    print(f"总容量: {sum(memory_storage.values())}/{MEMORY_CAPACITY}")
    print(f"物料详情:")
    for mat_type, count in memory_storage.items():
        print(f"  材料类型 {mat_type}: {count} 个")


# ==================== 创建工作站实例 ====================
# 创建6个工作站，每种类型的3D打印机两个
workstations = []
printer_types = [1, 1, 1, 1, 1, 1]  # 每种类型2个

for i in range(TOTAL_WORKSTATIONS):
    ws_id = f"WS-{i + 1:02d}"
    printer_type = printer_types[i]
    station = WorkStation(ws_id, printer_type)
    # 初始化cache区物料（尽可能平均分配）
    # 每个工作站cache容量3，6个工作站共18个物料，10种材料平均分配
    mapping = {
        0: [1, 2, 3],
        1: [4, 5, 6],
        2: [7, 8, 9],
        3: [10, 1, 2],
        4: [3, 4, 5],
        5: [6, 7, 8],
    }
    for idx in mapping.get(i, []):
        station.cache_area[idx] = 1
    workstations.append(station)
# endregion

# region 系统流
#订单队列
unreleased_orders=[]
released_orders=[]
suspended_orders=[]
completed_orders=[]
#作业队列
global_ready_queues=[]
ready_suspended_queues=[]
blocked_suspended_queues=[]
blocked_queues=[]


#检查所有订单是否已完成
def check_all_orders_completed(orders):
    for order in orders:
        if order["status"] != "completed":
            return False
    return True


#检查订单中的所有作业是否已完成
def check_all_jobs_in_order_completed(order):
    for job in order["jobs"]:
        if job["status"] != "completed":
            return False
    return True


#检查作业中的所有操作是否已完成
def check_all_operations_in_job_completed(job):
    for operation in job["operations"]:
        if operation["status"] != "completed":
            return False
    return True


def get_ready_operation(job):
    """
    获取job中第一个ready状态的操作
    :param job: 作业对象
    :return: ready的操作对象，如果没有则返回None
    """
    for operation in job["operations"]:
        if operation["status"] == "ready":
            return operation
    return None


# 主系统流
def run():
    """
    主仿真循环
    """
    global current_time
    print("=" * 80)
    print("开始仿真")
    print("=" * 80)

    # 1. 初始化事件队列：为每个工作站创建初始OOOE事件
    initialize_events()

    # 2. 初始化订单队列
    initialize_order_queues()
    LS()
    # 为所有JU事件初始化status
    for order in orders:
        for job in order["jobs"]:
            if "JU" in job and job["JU"]:
                for ju in job["JU"]:
                    ju["status"] = "untriggered"
    # 3. 主循环：处理事件直到所有订单完成
    iteration = 0
    while not check_all_orders_completed(orders):
        # time.sleep(0.01)
        # if iteration== 1200:
        #     time.sleep(10)
        iteration += 1
        if VERBOSE:
            print(f"{'=' * 80}")
            print(f"迭代 {iteration}")
            print(f"{'=' * 80}")
            print(f"事件队列：", end="")
            for event in sorted(event_queue.queue, key=lambda e: (e.time, e.prior, e.counter)):
                print(f"{event.event_type} {event.time:.2f}", end=", ")
        # 获取下一个事件
        next_event = event_queue.get_next_event()
        if next_event is None:
            print("\n警告：事件队列为空但订单未完成！")
            break
        # 推进仿真时钟
        current_time = next_event.time
        print()
        print(f"当前时间: {current_time}")
        # 记录本轮开始前已完成的订单数量
        completed_orders_before = len([o for o in orders if o["status"] == "completed"])
        # 处理事件
        if next_event.event_type == "order_arrival":
            print(f"到达订单为{next_event.data["order"]}")
            handle_order_arrival(next_event)
        elif next_event.event_type == "uncertainty_OU":
            print(f"订单不确定性：相关订单: {next_event.data["order"]}")
            handle_uncertainty_OU(next_event)
        elif next_event.event_type == "logistics_start":
            log_type = next_event.data.get("logistics_type")
            if log_type == "memory_cache_transfer":
                print(
                    f"物流：内存→缓存  |  物料类型={next_event.data.get('material_type')}  数量={next_event.data.get('quantity')}")
            elif log_type == "harddisk_memory_transfer":
                print(
                    f"物流：硬盘→内存  |  物料类型={next_event.data.get('material_type')}  数量={next_event.data.get('quantity')}")
            elif log_type == "memory_cache_exchange":
                print(f"物流：内存↔缓存  |  工作站={next_event.data.get('workstation')}, "
                      f"内存物料={next_event.data.get('material_from_memory')}, "
                      f"缓存物料={next_event.data.get('material_from_cache')}, "
                      f"数量={next_event.data.get('quantity')}")
            elif log_type == "memory_harddisk_exchange":
                print(f"物流：内存↔硬盘  |  内存物料={next_event.data.get('material_from_memory')}, "
                      f"硬盘物料={next_event.data.get('material_from_harddisk')}, "
                      f"数量={next_event.data.get('quantity')}")
            elif log_type == "wip_transfer":
                print(f"物流：WIP转移  |  起点={next_event.data.get('source_workstation')}, "
                      f"终点={next_event.data.get('target_workstation')}, "
                      f"WIP={next_event.data.get('wip')}")
            handle_logistics_start(next_event)
        elif next_event.event_type == "logistics_end":
            log_type = next_event.data.get("logistics_type")
            if log_type == "memory_cache_transfer":
                print(
                    f"物流：内存→缓存  |  物料类型={next_event.data.get('material_type')}  数量={next_event.data.get('quantity')}")
            elif log_type == "harddisk_memory_transfer":
                print(
                    f"物流：硬盘→内存  |  物料类型={next_event.data.get('material_type')}  数量={next_event.data.get('quantity')}")
            elif log_type == "memory_cache_exchange":
                print(f"物流：内存↔缓存  |  工作站={next_event.data.get('workstation')}, "
                      f"内存物料={next_event.data.get('material_from_memory')}, "
                      f"缓存物料={next_event.data.get('material_from_cache')}, "
                      f"数量={next_event.data.get('quantity')}")
            elif log_type == "memory_harddisk_exchange":
                print(f"物流：内存↔硬盘  |  内存物料={next_event.data.get('material_from_memory')}, "
                      f"硬盘物料={next_event.data.get('material_from_harddisk')}, "
                      f"数量={next_event.data.get('quantity')}")
            elif log_type == "wip_transfer":
                print(f"物流：WIP转移  |  起点={next_event.data.get('source_workstation')}, "
                      f"终点={next_event.data.get('target_workstation')}, "
                      f"WIP={next_event.data.get('wip')}")
            handle_logistics_end(next_event)
        elif next_event.event_type == "OOOE":
            print(f"OOOE事件相关工作站ws={next_event.data["workstation"].ws_id}")
            handle_OOOE(next_event)
        elif next_event.event_type == "operation_complete":
            print(f"操作完成：工作站{next_event.data["workstation"].ws_id},作业编号{next_event.data["job"]},操作编号{next_event.data["operation"]}")
            handle_operation_completion(next_event)
        elif next_event.event_type == "uncertainty_start" and next_event.data["uncertainty_type"] == "EU":
            print(f"不确定性开始：类型{next_event.data["uncertainty_type"]},信息{next_event.data["eu_data"]}")
            handle_uncertainty_start(next_event)
        elif next_event.event_type == "uncertainty_start" and next_event.data["uncertainty_type"] == "JU":
            print(f"不确定性开始：类型{next_event.data["uncertainty_type"]},信息{next_event.data["ju_data"]}")
            handle_uncertainty_start(next_event)
        elif next_event.event_type == "uncertainty_end" and next_event.data["uncertainty_type"] == "EU":
            print(f"不确定性结束：类型{next_event.data["uncertainty_type"]},信息{next_event.data["eu_data"]}")
            handle_uncertainty_end(next_event)
        elif next_event.event_type == "uncertainty_end" and next_event.data["uncertainty_type"] == "JU":
            print(f"不确定性结束：类型{next_event.data["uncertainty_type"]},信息{next_event.data["ju_data"]}")
            handle_uncertainty_end(next_event)
        elif next_event.event_type == "uncertainty_JU":
            print(f"Job绑定的不确定性：工作站{next_event.data["workstation"].ws_id},作业编号{next_event.data["job"]},操作编号{next_event.data["operation"]},信息{next_event.data["ju_data"]}")
            handle_uncertainty_JU(next_event)
        elif next_event.event_type == "LS_periodic":
            handle_LS_periodic(next_event)
        elif next_event.event_type == "MS_periodic":
            handle_MS_periodic(next_event)
        elif next_event.event_type == "SS_periodic":
            handle_SS_periodic(next_event)
        # 检查本轮是否有订单完成
        completed_orders_after = len([o for o in orders if o["status"] == "completed"])
        if completed_orders_after > completed_orders_before:
            print(f"\n本轮有订单完成，触发长程调度(LS)")
            LS()
        # 定期输出状态（仅在VERBOSE模式下）
        if VERBOSE:
            if iteration % 1 == 0:
                print_schedule_status()
                check_system_state()
            display_memory_status()
    print("\n" + "=" * 80)
    print("仿真结束！所有订单已完成")
    print("=" * 80)
    print(f"总仿真时间: {current_time}")
    # 最终状态检查
    check_final_state()
    # 输出订单执行情况汇总
    print_order_execution_summary()
    # 计算结果指标
    calculate_metrics()


def initialize_events():
    """
    初始化事件队列，为每个工作站创建初始OOOE事件，并为未到达的订单创建订单到达事件，以及添加EU事件
    """
    print("\n初始化事件队列...")
    # 为每个工作站创建初始OOOE事件
    for ws in workstations:
        create_OOOE_event(ws)
    # 为未到达的订单创建订单到达事件
    order_arrival_count = 0
    for order in orders:
        if order["arrival_time"] > 0:
            event = Event(
                event_type="order_arrival",
                time=order["arrival_time"],
                order=order
            )
            event_queue.add_event(event)
            order_arrival_count += 1
    print(f"创建了 {order_arrival_count} 个订单到达事件")
    # 添加EU事件（环境级不确定性事件）
    eu_count = 0
    for eu in EU_events:
        # 创建EU开始事件
        start_event = Event(
            event_type="uncertainty_start",
            time=eu["start_time"],
            uncertainty_type="EU",
            eu_data=eu
        )
        event_queue.add_event(start_event)

        # 创建EU结束事件
        end_event = Event(
            event_type="uncertainty_end",
            time=eu["start_time"] + eu["duration"],
            uncertainty_type="EU",
            eu_data=eu
        )
        event_queue.add_event(end_event)
        eu_count += 1
    print(f"创建了 {eu_count * 2} 个EU不确定性事件（{eu_count}个开始+{eu_count}个结束）")
    # 添加初始周期性事件
    event_queue.add_event(Event(
        event_type="LS_periodic",
        time=ps.LS_periodic_interval
    ))
    event_queue.add_event(Event(
        event_type="MS_periodic",
        time=ps.MS_periodic_interval
    ))
    event_queue.add_event(Event(
        event_type="SS_periodic",
        time=ps.SS_periodic_interval
    ))
    print(f"创建了 3 个初始周期性事件 (LS/MS/SS)")

    print(f"初始化完成，事件队列共 {len(workstations) + order_arrival_count + eu_count * 2} 个事件")


def initialize_order_queues():
    """
    初始化订单队列，将当前时刻（时刻0）已到达的订单放入unreleased_orders，并检查OU事件
    """
    global unreleased_orders
    print("\n初始化订单队列...")
    for order in orders:
        if order["arrival_time"] == 0:
            order["status"] = "unreleased"
            unreleased_orders.append(order)
            # 检查并添加OU事件
            check_and_add_OU_event(order)
    print(f"初始化完成，{len(unreleased_orders)} 个订单（arrival_time=0）进入unreleased队列")


def handle_order_arrival(event):
    """
    处理订单到达事件
    :param event: 订单到达事件对象
    """
    order = event.data["order"]
    print(f"\n订单到达: Order {order['order_id']} @ 时间 {current_time}")
    # 将订单加入unreleased_orders队列
    order["status"] = "unreleased"
    unreleased_orders.append(order)
    print(f"  订单 {order['order_id']} 加入unreleased队列")
    # 检查并添加OU事件（后到达订单的OU也需调度，与initialize_order_queues保持一致）
    check_and_add_OU_event(order)
    # 执行长程调度
    print(f"  执行长程调度...")
    LS()


def create_OOOE_event(ws):
    """
    创建OOOE事件
    :param ws: 工作站对象
    """
    oooe_event = Event(
        event_type="OOOE",
        time=current_time,
        workstation=ws
    )
    event_queue.add_event(oooe_event)
    print(f"    创建OOOE事件 @ {ws.ws_id}")


def handle_LS_periodic(event):
    """处理LS周期性触发事件"""
    print(f"\n[LS周期性触发] @ 时间 {current_time}")
    LS()
    # 安排下一次LS周期性事件
    if not check_all_orders_completed(orders):
        next_event = Event(
            event_type="LS_periodic",
            time=current_time + ps.LS_periodic_interval
        )
        event_queue.add_event(next_event)


def handle_MS_periodic(event):
    """处理MS周期性触发事件"""
    print(f"\n[MS周期性触发] @ 时间 {current_time}")
    MS()
    # 安排下一次MS周期性事件
    if not check_all_orders_completed(orders):
        next_event = Event(
            event_type="MS_periodic",
            time=current_time + ps.MS_periodic_interval
        )
        event_queue.add_event(next_event)


def handle_SS_periodic(event):
    """处理SS周期性触发事件"""
    print(f"\n[SS周期性触发] @ 时间 {current_time}")
    SS()
    # 安排下一次SS周期性事件
    if not check_all_orders_completed(orders):
        next_event = Event(
            event_type="SS_periodic",
            time=current_time + ps.SS_periodic_interval
        )
        event_queue.add_event(next_event)

# 检查工作站内局部队列是否存在可执行的操作
def handle_OOOE(event):
    """
    检查工作站的job队列，尝试执行满足条件的操作
    :param ws: 工作站对象
    """
    # 重新排序队列（基于laxity）
    ws = event.data["workstation"]
    ws.sort_job_queues(current_time)
    executed = True
    # 循环直到没有可执行的操作
    while executed:
        executed = False
        # 遍历队列中的每个job
        for job in ws.job_queue:
            # 获取该job中第一个ready的操作
            operation = get_ready_operation(job)
            if operation is None:
                continue  # 该job没有ready的操作，检查下一个job
            # 检查资源是否可用
            if ws.check_resource_available(operation, job):
                # 资源可用，开始执行
                print(f"\n开始执行操作: {operation['o_id']}")
                print(f"  作业: {job['job_id']}")
                print(f"  操作类型: {operation['o_type']}")
                # 占用资源并获取执行时间
                exec_time = ws.occupy_resource(operation)
                # 更新操作状态
                operation["status"] = "running"
                # 创建操作完成事件
                completion_time = current_time + exec_time
                completion_event = Event(
                    event_type="operation_complete",
                    time=completion_time,
                    workstation=ws,
                    job=job,
                    operation=operation
                )
                # 添加该操作相关的JU事件
                add_JU_events_for_operation(job, operation, ws, current_time, completion_time)
                event_queue.add_event(completion_event)
                print(f"  预计完成时间: {completion_time}")
                executed = True
                # 继续检查是否还有其他可执行的操作
                continue
        if not executed:
            print(f"  {ws.ws_id} 暂无可执行操作")
    # 检查是否有空闲资源，如果有则尝试工作窃取
    idle_resources = get_idle_resources(ws)
    if idle_resources:
        print(f"\n{ws.ws_id} 存在 {len(idle_resources)} 个空闲资源，尝试工作窃取...")
        for idle_res in idle_resources:
            resource_type = idle_res["resource_type"]
            result = try_work_stealing(ws, resource_type)
            if result == "success_immediate":
                # 仅在可立即执行时创建OOOE
                oooe_event = Event(
                    event_type="OOOE",
                    time=current_time,
                    workstation=ws
                )
                event_queue.add_event_front(oooe_event)
                print(f"    创建OOOE事件 @ {ws.ws_id}")
            # success_pending 或 failed：不创建OOOE，由物流结束时自然触发


# 处理操作完成事件
def handle_operation_completion(event):
    """
    处理操作完成事件
    :param event: 完成事件对象
    """
    ws = event.data["workstation"]
    job = event.data["job"]
    operation = event.data["operation"]
    print(f"\n操作完成: {operation['o_id']} @ {ws.ws_id}")
    # 更新操作状态
    if operation["o_type"] == "D":
        # D操作需要区分ID和MD
        if operation.get("ID_started", False) and not operation.get("ID_completed", False):
            # ID完成
            operation["ID_completed"] = True
            operation["status"] = "ready"
            print(f"  ID步骤完成")
            # 释放ID资源
            ws.release_resource(operation)
        elif operation.get("MD_started", False) and not operation.get("MD_completed", False):
            # MD完成，整个D操作完成
            operation["MD_completed"] = True
            operation["status"] = "completed"
            print(f"  MD步骤完成，D操作完成")
            # MD完成后，标记该job的P操作为ready
            for op in job["operations"]:
                if op["o_type"] == "P" and op["status"] == "unready":
                    op["status"] = "ready"
            # 释放MD资源
            ws.release_resource(operation)
    else:
        # P和A操作直接完成
        operation["status"] = "completed"
        ws.release_resource(operation)
        # 创建资源空闲事件
        if operation["o_type"] == "P":
            # P操作完成后，WIP存入临时存储区
            ws.temp_wip_storage.append(operation["o_id"])
            # P完成后，标记该job的A操作为ready
            for op in job["operations"]:
                if op["o_type"] == "A" and op["status"] == "unready":
                    op["status"] = "ready"
            # print(f"  WIP {operation['o_id']} 存入 {ws.ws_id} 临时存储区")
    # 检查job是否完成
    if check_all_operations_in_job_completed(job):
        job["status"] = "completed"
        job_id = job["job_id"]
        job_completion_times[job_id] = current_time  # 记录job完成时间
        print(f"  作业完成: {job['job_id']}")
        # 从工作站队列中移除
        ws.pop_job(job)
        # 检查order是否完成
        order_id = int(job["job_id"].split('_')[0])
        for order in orders:
            if order["order_id"] == order_id:
                if check_all_jobs_in_order_completed(order):
                    order["status"] = "completed"
                    order_completion_times[order_id] = current_time  # 记录order完成时间
                    released_orders.remove(order)
                    completed_orders.append(order)
                    print(f"  订单完成: Order {order_id}")
                break
    # 创建该工作站的OOOE事件
    create_OOOE_event(ws)


def get_unavailable_resources():
    """
    获取每个工作站当前因EU事件而不可用的资源
    返回：{ws_id: set(unavailable_resource_types)}

    判断逻辑：EU正在进行的条件是 start_time <= current_time < start_time + duration
    即：EU已开始（start_time<=current_time）且未结束（结束事件仍在队列中）
    """
    unavailable = {ws.ws_id: set() for ws in workstations}

    # 遍历事件队列，查找正在进行的EU事件
    for event in event_queue.queue:
        if event.event_type == "uncertainty_end" and event.data.get("uncertainty_type") == "EU":
            eu_data = event.data.get("eu_data")
            if eu_data:
                # 关键修复：只有已经开始的EU才会影响当前资源可用性
                if eu_data.get("start_time", float('inf')) > current_time:
                    continue  # 未来才开始的EU，跳过

                location = eu_data.get("location")
                if location:
                    affected_ws, affected_resource = parse_EU_location(location)
                    if affected_ws and affected_resource:
                        unavailable[affected_ws.ws_id].add(affected_resource)

    return unavailable

#长程调度
def LS():
    global order_release_times
    def get_order_ids(order_list):
        return [o.get("order_id") for o in order_list]
    def get_job_ids(job_list):
        return [j.get("job_id") for j in job_list]
    print("----【执行 LLF 调度 前 / Before LLF Scheduling】----")
    print(f"未释放订单 IDs: {get_order_ids(unreleased_orders)}")
    print(f"已释放订单 IDs: {get_order_ids(released_orders)}")
    if isinstance(global_ready_queues, dict):
        print("全局就绪队列(按设备) / Ready Queues (by WS):")
        for ws, q in global_ready_queues.items():
            print(f"  WS {ws}: {get_job_ids(q)}")
    elif isinstance(global_ready_queues, list):
        print(f"全局就绪队列 IDs: {get_job_ids(global_ready_queues)}")
    # --- 保存前状态 ---
    before_unreleased = unreleased_orders.copy()
    before_released = released_orders.copy()
    if isinstance(global_ready_queues, dict):
        before_ready = {k: v.copy() for k, v in global_ready_queues.items()}
    else:
        before_ready = global_ready_queues.copy()
    print("\n>>> 开始执行 LLF 调度 / Start LLF Scheduling >>>\n")
    # 执行 LLF 调度
    LLF_LS6.llf_schedule(
        current_time,
        unreleased_orders,
        normal_released_capacity,
        max_released_order_capacity,
        released_orders,
        global_ready_queues,
        order_release_times
    )
    print(current_time)

    print("\n>>> LLF 调度结束 / LLF Scheduling Finished >>>")
    print("----【执行后状态 / After LLF Scheduling】----")
    print(f"未释放订单 IDs: {get_order_ids(unreleased_orders)}")
    print(f"已释放订单 IDs: {get_order_ids(released_orders)}")

    if isinstance(global_ready_queues, dict):
        print("全局就绪队列(按设备):")
        for ws, q in global_ready_queues.items():
            print(f"  WS {ws}: {get_job_ids(q)}")
    else:
        print(f"全局就绪队列 IDs: {get_job_ids(global_ready_queues)}")

    print("\n----【变化情况 / Changes Detected】----")

    # Orders变化
    print(f"未释放订单新增 / Added to Unreleased: "
          f"{[o.get('order_id') for o in unreleased_orders if o not in before_unreleased]}")
    print(f"未释放订单移除 / Removed from Unreleased: "
          f"{[o.get('order_id') for o in before_unreleased if o not in unreleased_orders]}")

    print(f"新加入已释放订单 / Newly Released Orders: "
          f"{[o.get('order_id') for o in released_orders if o not in before_released]}")

    # Ready Queue变化
    if isinstance(global_ready_queues, dict):
        for ws in before_ready.keys():
            before_ids = set(get_job_ids(before_ready[ws]))
            after_ids = set(get_job_ids(global_ready_queues[ws]))

            added = list(after_ids - before_ids)
            removed = list(before_ids - after_ids)

            if added or removed:
                print(f"  WS {ws} Ready Queue Change → Added: {added}, Removed: {removed}")
    else:
        before_ids = set(get_job_ids(before_ready))
        after_ids = set(get_job_ids(global_ready_queues))
        print(f"Ready Queue Added: {list(after_ids - before_ids)}")
        print(f"Ready Queue Removed: {list(before_ids - after_ids)}")

    print("\n>>> 调用 MS 调度... / Call MS Scheduling... >>>\n")
    MS()


#中程调度
def MS():
    """
    中程调度：处理两对队列的suspend/resume
    """
    print("\n>>> 调用 MS 调度... / Call MS Scheduling... >>>\n")
    JT_MS.mid_term_scheduling(
        global_ready_queues, ready_suspended_queues,
        ps.MS_threshold, current_time, orders
    )
    JT_MS.mid_term_scheduling(
        blocked_queues, blocked_suspended_queues,
        ps.MS_threshold, current_time, orders
    )
    SS()


def SS():
    """
    短程调度
    """
    print("\n>>> 调用 SS 调度... / Call SS Scheduling... >>>\n")
    global global_ready_queues

    # 获取当前因EU不可用的资源
    unavailable_resources = get_unavailable_resources()

    # 调用亲和性调度（返回 modified_ws_ids 和 pending_jobs）
    modified_ws_ids, pending_jobs = PA_SS2.affinity_based_scheduling(
        global_ready_queues, workstations, unavailable_resources
    )

    # 为修改的工作站创建OOOE事件
    for ws in workstations:
        if ws.ws_id in modified_ws_ids:
            oooe_event = Event(
                event_type="OOOE",
                time=current_time,
                workstation=ws
            )
            event_queue.add_event(oooe_event)

    # 处理延迟作业，触发物流事件
    if pending_jobs:
        trigger_logistics_for_pending_jobs(pending_jobs)

    return modified_ws_ids


def get_materials_in_transit():
    """
    获取当前事件队列中正在运送的材料（用于避免重复触发物流）
    检查所有logistics_end事件中的"换入材料"
    """
    materials = set()
    for event in event_queue.queue:
        if event.event_type != "logistics_end":
            continue
        logistics_type = event.data.get("logistics_type")
        if logistics_type in ["memory_cache_transfer", "harddisk_cache_transfer"]:
            # 单向运送：material_type 即换入材料
            mat = event.data.get("material_type")
            if mat is not None:
                materials.add(mat)
        elif logistics_type == "memory_cache_exchange":
            # 换入到cache的是 material_from_memory
            mat = event.data.get("material_from_memory")
            if mat is not None:
                materials.add(mat)
        elif logistics_type == "cache_harddisk_exchange":
            # 换入到cache的是 material_from_harddisk
            mat = event.data.get("material_from_harddisk")
            if mat is not None:
                materials.add(mat)
        elif logistics_type == "steal_memory_cache_exchange":
            mat = event.data.get("material_from_memory")
            if mat is not None:
                materials.add(mat)
        elif logistics_type == "steal_cache_harddisk_exchange":
            mat = event.data.get("material_from_harddisk")
            if mat is not None:
                materials.add(mat)
    return materials


def compute_useless_materials_per_ws():
    """
    为每个工作站计算"无用材料"集合
    无用材料：该ws的cache中存在 且 该ws的LRQ中没有任何未完成P操作需要它
    返回：{ws_id: set of useless materials}
    """
    result = {}
    for ws in workstations:
        # 收集该ws LRQ中所有未完成P操作所需的材料
        needed_in_ws = set()
        for job in ws.job_queue:
            for op in job["operations"]:
                if op["o_type"] == "P" and op["status"] != "completed":
                    needed_in_ws.add(op["material"])
        # cache中的材料减去需要的 = 无用材料
        cache_materials = {mat for mat, count in ws.cache_area.items() if count > 0}
        useless = cache_materials - needed_in_ws
        result[ws.ws_id] = useless
    return result


def count_p_operations_in_ws(ws):
    """统计ws的LRQ中未完成P操作的数量"""
    count = 0
    for job in ws.job_queue:
        for op in job["operations"]:
            if op["o_type"] == "P" and op["status"] != "completed":
                count += 1
    return count


def select_swap_out_material_and_ws(useless_materials_per_ws):
    """
    选择换出材料和换出工作站
    两层排序：
        一级键：该材料在所有ws"无用材料"中的总数量（按cache个数）降序
        二级键：该材料在所有ws cache中的总数量降序
    在拥有该材料的ws中，选未完成P操作最少的作为换出工作站

    返回 (material_out, source_ws) 或 None
    """
    # 收集所有候选无用材料
    all_useless_materials = set()
    for materials in useless_materials_per_ws.values():
        all_useless_materials.update(materials)

    if not all_useless_materials:
        return None

    # 计算每种材料的两层key
    material_scores = []
    for material in all_useless_materials:
        # 一级：该材料在所有ws无用材料中的总数量（按cache个数）
        useless_count = sum(
            ws.cache_area.get(material, 0) for ws in workstations
            if material in useless_materials_per_ws.get(ws.ws_id, set())
        )
        # 二级：该材料在所有ws cache中的总数量
        total_in_cache = sum(ws.cache_area.get(material, 0) for ws in workstations)
        material_scores.append((material, useless_count, total_in_cache))

    # 按一级降序、二级降序排序
    material_scores.sort(key=lambda x: (-x[1], -x[2]))
    selected_material = material_scores[0][0]

    # 找出该材料处于"无用"状态的所有ws
    candidate_ws = [ws for ws in workstations
                    if selected_material in useless_materials_per_ws.get(ws.ws_id, set())]

    if not candidate_ws:
        return None

    # 选未完成P操作最少的ws
    candidate_ws.sort(key=lambda ws: count_p_operations_in_ws(ws))
    source_ws = candidate_ws[0]

    return selected_material, source_ws


def trigger_logistics_for_pending_jobs(pending_jobs):
    """
    为延迟分配的作业触发物流事件
    1. 按所需材料去重
    2. 过滤掉已在运送中的材料
    3. 每种材料触发一次物流事件
    """
    print(f"\n  >>> 为 {len(pending_jobs)} 个延迟作业触发物流事件")

    # 1. 收集所有需要的材料（去重）
    needed_materials = set()
    for job in pending_jobs:
        material = next(
            (op["material"] for op in job["operations"] if op["o_type"] == "P"),
            None
        )
        if material is not None:
            needed_materials.add(material)

    # 2. 过滤掉已在运送中的材料
    materials_in_transit = get_materials_in_transit()
    needed_materials -= materials_in_transit

    if not needed_materials:
        print(f"    所有所需材料已在运送中，跳过")
        return

    print(f"    需要触发的材料: {needed_materials}")

    # 3. 计算各ws的"无用材料"（在循环中动态更新，避免重复选择）
    useless_materials_per_ws = compute_useless_materials_per_ws()

    # 4. 为每种需要的材料触发物流
    for material_in in needed_materials:
        result = select_swap_out_material_and_ws(useless_materials_per_ws)
        if result is None:
            print(f"    没有可用的'无用材料'换出，跳过 材料{material_in}（选项B：等待自然释放）")
            continue
        material_out, source_ws = result

        # 触发物流交换事件
        if memory_storage.get(material_in, 0) > 0:
            create_logistics_event_memory_cache_exchange(
                source_ws, material_in, material_out, 1
            )
        else:
            create_logistics_event_cache_harddisk_exchange(
                source_ws, material_out, material_in, 1
            )

        print(f"    触发物流: {source_ws.ws_id} cache换出{material_out} ↔ 换入{material_in}")

        # 从无用材料集合中移除已选定的材料和工作站组合，避免下次重复选
        if material_out in useless_materials_per_ws.get(source_ws.ws_id, set()):
            useless_materials_per_ws[source_ws.ws_id].discard(material_out)


#异常检查
def check_system_state():
    """
    检查当前系统状态是否存在异常
    在每次迭代中调用，发现异常时打印警告信息
    """
    has_error = False

    for ws in workstations:
        # 检查1: 是否存在同类操作并行运行
        running_operations = {
            "ID": [],
            "MD": [],
            "P": [],
            "A": []
        }

        for job in ws.job_queue:
            for operation in job["operations"]:
                if operation["status"] == "running":
                    o_type = operation["o_type"]
                    if o_type == "D":
                        if operation.get("ID_started", False) and not operation.get("ID_completed", False):
                            running_operations["ID"].append(operation["o_id"])
                        if operation.get("MD_started", False) and not operation.get("MD_completed", False):
                            running_operations["MD"].append(operation["o_id"])
                    elif o_type == "P":
                        running_operations["P"].append(operation["o_id"])
                    elif o_type == "A":
                        running_operations["A"].append(operation["o_id"])

        # 检查是否有多个同类操作在运行
        for op_type, op_list in running_operations.items():
            if len(op_list) > 1:
                print(f"⚠️ 异常警告: {ws.ws_id} 存在多个{op_type}操作同时运行: {op_list}")
                has_error = True

        # 检查2: 资源busy值是否为0或1（跳过EU影响的资源）
        # 先检查当前是否有EU事件影响该工作站的资源
        eu_affected_resources = set()
        for event in event_queue.queue:
            if event.event_type == "uncertainty_end" and event.data.get("uncertainty_type") == "EU":
                eu_data = event.data.get("eu_data")
                if eu_data:
                    # 修复：只考虑已经开始的EU
                    if eu_data.get("start_time", float('inf')) > current_time:
                        continue
                    location = eu_data.get("location")
                    if location:
                        affected_ws, affected_resource = parse_EU_location(location)
                        if affected_ws == ws:
                            eu_affected_resources.add(affected_resource)

        resource_checks = [
            ("id_resource_busy", ws.id_resource_busy, "id_resource"),
            ("md_resource_busy", ws.md_resource_busy, "md_resource"),
            ("printer_3d_busy", ws.printer_3d_busy, "printer_3d"),
            ("assembly_resource_busy", ws.assembly_resource_busy, "assembly_resource")
        ]

        for resource_name, busy_value, resource_type in resource_checks:
            # 如果该资源正被EU影响，跳过检查
            if resource_type in eu_affected_resources:
                continue

            if busy_value not in [0, 1]:
                print(f"⚠️ 异常警告: {ws.ws_id} 的 {resource_name} 值异常: {busy_value} (应为0或1)")
                has_error = True

        # 检查3: 是否存在前序操作未完成，后序操作running/completed的情况
        for job in ws.job_queue:
            operations = job["operations"]
            for i in range(len(operations)):
                current_op = operations[i]
                current_status = current_op["status"]

                # 检查前序操作
                if i > 0:
                    prev_op = operations[i - 1]
                    prev_status = prev_op["status"]

                    # 如果当前操作是running或completed，前序操作必须是completed
                    if current_status in ["running", "completed"] and prev_status != "completed":
                        print(f"⚠️ 异常警告: {ws.ws_id} Job {job['job_id']} 操作顺序异常: "
                              f"{current_op['o_id']}({current_status}) 但前序操作 {prev_op['o_id']}({prev_status})")
                        has_error = True

                # 对于D操作，检查ID和MD的顺序
                if current_op["o_type"] == "D":
                    id_completed = current_op.get("ID_completed", False)
                    md_started = current_op.get("MD_started", False)
                    md_completed = current_op.get("MD_completed", False)

                    # 如果MD已开始或完成，ID必须已完成
                    if (md_started or md_completed) and not id_completed:
                        print(f"⚠️ 异常警告: {ws.ws_id} Job {job['job_id']} D操作内部顺序异常: "
                              f"MD已{'完成' if md_completed else '开始'} 但ID未完成")
                        has_error = True

        # 检查4: 资源占用与实际running操作的一致性
        # 统计实际running的操作数量
        actual_running = {
            "id_resource": 0,
            "md_resource": 0,
            "printer_3d": 0,
            "assembly_resource": 0
        }

        for job in ws.job_queue:
            for operation in job["operations"]:
                if operation["status"] == "running":
                    o_type = operation["o_type"]
                    if o_type == "D":
                        if operation.get("ID_started", False) and not operation.get("ID_completed", False):
                            actual_running["id_resource"] += 1
                        if operation.get("MD_started", False) and not operation.get("MD_completed", False):
                            actual_running["md_resource"] += 1
                    elif o_type == "P":
                        actual_running["printer_3d"] += 1
                    elif o_type == "A":
                        actual_running["assembly_resource"] += 1

        # 比对资源busy值与实际running数量（跳过EU影响的资源）
        if "id_resource" not in eu_affected_resources:
            if actual_running["id_resource"] != ws.id_resource_busy:
                print(f"⚠️ 异常警告: {ws.ws_id} id_resource_busy不一致: "
                      f"记录值={ws.id_resource_busy}, 实际running={actual_running['id_resource']}")
                has_error = True

        if "md_resource" not in eu_affected_resources:
            if actual_running["md_resource"] != ws.md_resource_busy:
                print(f"⚠️ 异常警告: {ws.ws_id} md_resource_busy不一致: "
                      f"记录值={ws.md_resource_busy}, 实际running={actual_running['md_resource']}")
                has_error = True

        if "printer_3d" not in eu_affected_resources:
            if actual_running["printer_3d"] != ws.printer_3d_busy:
                print(f"⚠️ 异常警告: {ws.ws_id} printer_3d_busy不一致: "
                      f"记录值={ws.printer_3d_busy}, 实际running={actual_running['printer_3d']}")
                has_error = True

        if "assembly_resource" not in eu_affected_resources:
            if actual_running["assembly_resource"] != ws.assembly_resource_busy:
                print(f"⚠️ 异常警告: {ws.ws_id} assembly_resource_busy不一致: "
                      f"记录值={ws.assembly_resource_busy}, 实际running={actual_running['assembly_resource']}")
                has_error = True

    return has_error


def check_final_state():
    """
    检查仿真结束时的最终状态
    在run()函数结束后调用
    """
    print(f"\n{'=' * 80}")
    print("最终状态检查")
    print(f"{'=' * 80}")

    has_error = False

    # 检查1: 所有OU是否已触发
    # print("\n1. 检查OU事件触发状态:")
    # untriggered_ou = []
    # for order in orders:
    #     if "OU" in order and order["OU"] is not None:
    #         ou = order["OU"]
    #         if ou.get("status") != "triggered":
    #             untriggered_ou.append(f"Order {order['order_id']}")
    #
    # if untriggered_ou:
    #     print(f"   ⚠️ 存在未触发的OU: {', '.join(untriggered_ou)}")
    #     has_error = True
    # else:
    #     print("   ✓ 所有OU已触发")

    # 检查2: 所有JU是否已触发
    print("\n2. 检查JU事件触发状态:")
    untriggered_ju = []
    for order in orders:
        for job in order["jobs"]:
            if "JU" in job and job["JU"]:
                for ju in job["JU"]:
                    if ju.get("status") != "triggered":
                        untriggered_ju.append(f"Job {job['job_id']} Type {ju['type']}")

    if untriggered_ju:
        print(f"   ⚠️ 存在未触发的JU: {', '.join(untriggered_ju)}")
        has_error = True
    else:
        print("   ✓ 所有JU已触发")

    # 检查3: 所有operation是否已完成
    print("\n3. 检查所有操作完成状态:")
    incomplete_operations = []
    for order in orders:
        for job in order["jobs"]:
            for operation in job["operations"]:
                if operation["status"] != "completed":
                    incomplete_operations.append(f"{operation['o_id']}({operation['status']})")

    if incomplete_operations:
        print(f"   ⚠️ 存在未完成的操作: {', '.join(incomplete_operations)}")
        has_error = True
    else:
        print("   ✓ 所有操作已完成")

    # 检查4: 所有工作站的局部队列是否为空
    print("\n4. 检查工作站局部队列:")
    non_empty_queues = []
    for ws in workstations:
        if len(ws.job_queue) > 0:
            non_empty_queues.append(f"{ws.ws_id}({len(ws.job_queue)}个job)")

    if non_empty_queues:
        print(f"   ⚠️ 存在非空局部队列: {', '.join(non_empty_queues)}")
        has_error = True
    else:
        print("   ✓ 所有工作站局部队列为空")

    # 检查5: 所有资源是否已空闲
    print("\n5. 检查工作站资源状态:")
    busy_resources = []
    for ws in workstations:
        if ws.id_resource_busy > 0:
            busy_resources.append(f"{ws.ws_id}.id_resource_busy={ws.id_resource_busy}")
        if ws.md_resource_busy > 0:
            busy_resources.append(f"{ws.ws_id}.md_resource_busy={ws.md_resource_busy}")
        if ws.printer_3d_busy > 0:
            busy_resources.append(f"{ws.ws_id}.printer_3d_busy={ws.printer_3d_busy}")
        if ws.assembly_resource_busy > 0:
            busy_resources.append(f"{ws.ws_id}.assembly_resource_busy={ws.assembly_resource_busy}")

    if busy_resources:
        print(f"   ⚠️ 存在忙碌资源: {', '.join(busy_resources)}")
        has_error = True
    else:
        print("   ✓ 所有资源已空闲")

    # 检查6: 全局就绪队列是否为空
    print("\n6. 检查全局就绪队列:")
    if len(global_ready_queues) > 0:
        print(f"   ⚠️ 全局就绪队列非空: {len(global_ready_queues)}个job")
        has_error = True
    else:
        print("   ✓ 全局就绪队列为空")

    # 总结
    print(f"\n{'=' * 80}")
    if has_error:
        print("❌ 最终状态检查发现异常")
    else:
        print("✅ 最终状态检查通过")
    print(f"{'=' * 80}\n")

    return not has_error


# region 工作窃取
def try_work_stealing(target_ws, resource_type):
    """
    统一的工作窃取流程
    返回：
        "success_immediate" - 窃取成功且可立即执行 → 调用方需创建OOOE
        "success_pending"   - 窃取成功但需等物流/WIP → 调用方不创建OOOE
        "failed"            - 未窃取成功
    """
    print(f"\n  尝试工作窃取: {target_ws.ws_id}, 资源类型: {resource_type}")

    # Step 1: 排序源工作站
    sorted_sources = sort_source_workstations(target_ws, resource_type)

    # Step 2: 依次检索，找第一个有候选的源ws
    source_ws = None
    candidates = []
    for ws in sorted_sources:
        cands = find_candidates_in_ws(ws, resource_type, target_ws)
        if cands:
            source_ws = ws
            candidates = cands
            break

    if source_ws is None:
        print(f"    未找到可窃取的候选作业")
        return "failed"

    # Step 3: 在选定ws中按 P_j = α·L_j + β·C_j 选最优
    selected_job, selected_cost = select_best_candidate(candidates, source_ws, target_ws, resource_type)
    if selected_job is None:
        print(f"    {source_ws.ws_id} 有候选但全部不可行（cost=inf）")
        return "failed"

    print(f"    选定作业 {selected_job['job_id']} 从 {source_ws.ws_id} 窃取到 {target_ws.ws_id}, cost={selected_cost}")

    # 执行窃取：作业转移
    source_ws.pop_job(selected_job)
    target_ws.add_job(selected_job, current_time)

    # 根据资源类型决定后续动作
    if resource_type in ["id_resource", "md_resource"]:
        return "success_immediate"

    elif resource_type == "printer_3d":
        material = next((op["material"] for op in selected_job["operations"] if op["o_type"] == "P"), None)
        # 条件1或2：可立即执行
        if target_ws.printer_material == material or target_ws.cache_area.get(material, 0) > 0:
            return "success_immediate"
        # 条件3：需要物流
        return trigger_steal_logistics_for_printer(target_ws, material)

    elif resource_type == "assembly_resource":
        # 触发WIP转移
        wip_id = next((op["o_id"] for op in selected_job["operations"] if op["o_type"] == "P"), None)
        create_logistics_event_wip_transfer(source_ws, target_ws, wip_id)
        return "success_pending"

    return "failed"


def count_unfinished_operations_for_resource(ws, resource_type):
    """
    统计ws的LRQ中，所需资源类型==resource_type的未完成等待中操作数量
    只统计status=ready或unready的操作（不含running、completed）
    """
    count = 0
    for job in ws.job_queue:
        for op in job["operations"]:
            if op["status"] not in ["ready", "unready"]:
                continue
            o_type = op["o_type"]
            if resource_type == "id_resource" and o_type == "D":
                if not op.get("ID_completed", False):
                    count += 1
            elif resource_type == "md_resource" and o_type == "D":
                if not op.get("MD_completed", False):
                    count += 1
            elif resource_type == "printer_3d" and o_type == "P":
                count += 1
            elif resource_type == "assembly_resource" and o_type == "A":
                count += 1
    return count


def sort_source_workstations(target_ws, resource_type):
    """
    Step 1: 排序源工作站
    一级键：该ws的resource_type是否故障（故障在前）
    二级键：未完成操作数量降序
    """
    unavailable_resources = get_unavailable_resources()
    sources = []
    for ws in workstations:
        if ws == target_ws:
            continue
        is_faulty = resource_type in unavailable_resources.get(ws.ws_id, set())
        unfinished_count = count_unfinished_operations_for_resource(ws, resource_type)
        sources.append((ws, is_faulty, unfinished_count))
    # 排序：故障在前（True > False，用 not is_faulty 让True排前）；数量降序
    sources.sort(key=lambda x: (not x[1], -x[2]))
    return [s[0] for s in sources]


def find_candidates_in_ws(source_ws, resource_type, target_ws):
    """
    Step 2: 在source_ws中找符合条件的候选作业
    通用条件：
        (1) job中存在某operation所需资源==resource_type
        (2) 该operation的前置已完成
        (3) 该operation未开始执行（status==ready且对应阶段未started）
    A资源额外条件：
        (4) job的P操作的WIP必须在source_ws.temp_wip_storage
    """
    candidates = []
    for job in source_ws.job_queue:
        # 找到job中需要resource_type的operation
        target_op = None
        for op in job["operations"]:
            o_type = op["o_type"]
            if resource_type == "id_resource" and o_type == "D":
                if not op.get("ID_started", False):
                    target_op = op
                    break
            elif resource_type == "md_resource" and o_type == "D":
                if op.get("ID_completed", False) and not op.get("MD_started", False):
                    target_op = op
                    break
            elif resource_type == "printer_3d" and o_type == "P":
                target_op = op
                break
            elif resource_type == "assembly_resource" and o_type == "A":
                target_op = op
                break
        if target_op is None:
            continue
        # 条件(3): operation状态为ready
        if target_op["status"] != "ready":
            continue
        # A资源额外条件: WIP必须在source_ws
        if resource_type == "assembly_resource":
            wip_in_source = False
            for op in job["operations"]:
                if op["o_type"] == "P":
                    if op["o_id"] in source_ws.temp_wip_storage:
                        wip_in_source = True
                    break
            if not wip_in_source:
                continue
        candidates.append(job)
    return candidates


def calculate_steal_cost(job, source_ws, target_ws, resource_type):
    """
    Step 3辅助：计算窃取成本C_j
    """
    if resource_type in ["id_resource", "md_resource"]:
        return 0
    elif resource_type == "printer_3d":
        material = next((op["material"] for op in job["operations"] if op["o_type"] == "P"), None)
        if material is None:
            return float('inf')
        # 条件1: 与load相同
        if target_ws.printer_material == material:
            return 0
        # 条件2: cache有
        if target_ws.cache_area.get(material, 0) > 0:
            return LOAD_TIME
        # 条件3: 需要物流
        if target_ws.logistics_for_stealing == 1:
            return float('inf')  # 物流通道已占用，不可行
        if memory_storage.get(material, 0) > 0:
            return MEMORY_TO_CACHE_TIME + LOAD_TIME
        else:
            return HARDDISK_TO_MEMORY_TIME + MEMORY_TO_CACHE_TIME + LOAD_TIME
    elif resource_type == "assembly_resource":
        return WIP_TRANSFER_TIME
    return float('inf')


def select_best_candidate(candidates, source_ws, target_ws, resource_type):
    """
    Step 3: 按 P_j = α·L_j + β·C_j 选最优候选
    返回 (best_job, best_cost) 或 (None, None) 若全部不可行
    """
    best_job = None
    best_priority = float('inf')
    best_cost = 0
    for job in candidates:
        L_j = get_job_priority(job, orders, current_time)
        C_j = calculate_steal_cost(job, source_ws, target_ws, resource_type)
        if C_j == float('inf'):
            continue  # 不可行的候选跳过
        P_j = ps.WS_ALPHA * L_j + ps.WS_BETA * C_j
        if P_j < best_priority:
            best_priority = P_j
            best_job = job
            best_cost = C_j
    return best_job, best_cost


def trigger_steal_logistics_for_printer(target_ws, material_in):
    """触发P资源窃取的物流交换：选最冗余材料换出，换入为目标作业的P材料
    换出材料只在'本站无用材料'(本站cache有、且本站job_queue中无任何未完成P需要它)中选；
    若无可换出材料，则不触发窃取，返回failed"""
    # 仅在本站"无用材料"中选换出，避免换走本站已分配作业仍需要的材料
    useless_in_target = compute_useless_materials_per_ws().get(target_ws.ws_id, set())
    available_cache_materials = [mat for mat in useless_in_target
                                 if target_ws.cache_area.get(mat, 0) > 0]
    if not available_cache_materials:
        return "failed"
    # 统计其他工作站cache中每种材料的总数
    other_ws_material_count = {}
    for ws in workstations:
        if ws == target_ws:
            continue
        for mat, count in ws.cache_area.items():
            other_ws_material_count[mat] = other_ws_material_count.get(mat, 0) + count
    # 找最冗余的材料
    material_redundancy = [(mat, other_ws_material_count.get(mat, 0)) for mat in available_cache_materials]
    material_redundancy.sort(key=lambda x: x[1], reverse=True)
    max_redundancy = material_redundancy[0][1]
    most_redundant_materials = [mat for mat, red in material_redundancy if red == max_redundancy]
    material_out = random.choice(most_redundant_materials)
    # 创建物流事件
    if memory_storage.get(material_in, 0) > 0:
        create_stealing_logistics_event_memory_cache_exchange(target_ws, material_in, material_out, 1)
    else:
        create_stealing_logistics_event_cache_harddisk_exchange(target_ws, material_in, material_out, 1)
    target_ws.logistics_for_stealing = 1
    return "success_pending"
# endregion


#得到当前的空闲资源 用于工作窃取触发
def get_idle_resources(ws):
    """
    检查所有工作站，找出所有空闲资源
    :param workstations: 工作站列表
    :return: 空闲资源列表，每个元素为字典 {"workstation": ws对象, "resource_type": 资源类型字符串, "idle_count": 空闲数量}
    """
    idle_resources = []
    # 检查ID资源
    idle_id = ws.id_resource - ws.id_resource_busy
    if idle_id > 0:
        idle_resources.append({
            "workstation": ws,
            "workstation_id": ws.ws_id,
            "resource_type": "id_resource",
            "idle_count": idle_id
        })
    # 检查MD资源
    idle_md = ws.md_resource - ws.md_resource_busy
    if idle_md > 0:
        idle_resources.append({
            "workstation": ws,
            "workstation_id": ws.ws_id,
            "resource_type": "md_resource",
            "idle_count": idle_md
        })
    # 检查3D打印机
    idle_printer = ws.printer_3d - ws.printer_3d_busy
    if idle_printer > 0:
        idle_resources.append({
            "workstation": ws,
            "workstation_id": ws.ws_id,
            "resource_type": "printer_3d",
            "printer_type": ws.printer_3d_type,  # 额外记录打印机类型
            "idle_count": idle_printer
        })
    # 检查装配资源
    idle_assembly = ws.assembly_resource - ws.assembly_resource_busy
    if idle_assembly > 0:
        idle_resources.append({
            "workstation": ws,
            "workstation_id": ws.ws_id,
            "resource_type": "assembly_resource",
            "idle_count": idle_assembly
        })
    return idle_resources


# 输出实时调度信息
def print_schedule_status():
    """
    输出当前调度状态
    """
    print(f"\n{'=' * 80}")
    print(f"当前调度状态 (时间: {current_time})")
    print(f"{'=' * 80}")

    # 统计订单状态
    order_stats = {"unreleased": 0, "released": 0, "suspended": 0, "completed": 0}
    for order in orders:
        status = order.get("status", "unreleased")
        order_stats[status] = order_stats.get(status, 0) + 1

    print(f"订单状态统计:")
    print(f"  未释放: {order_stats.get('unreleased', 0)}")
    print(f"  已释放: {order_stats.get('released', 0)}")
    print(f"  暂停: {order_stats.get('suspended', 0)}")
    print(f"  已完成: {order_stats.get('completed', 0)}")
    print(f"  总计: {len(orders)}")

    # 显示各工作站详细状态
    print(f"\n工作站详细状态:")
    for ws in workstations:
        print(f"\n  {'─' * 70}")
        print(f"  {ws.ws_id}: {len(ws.job_queue)} 个作业在队列")

        # 1. 局部队列中所有作业及其操作状态
        if ws.job_queue:
            print(f"    作业队列:")
            for job in ws.job_queue:
                job_id = job["job_id"]
                print(f"      Job {job_id}:")
                for op in job["operations"]:
                    op_id = op["o_id"]
                    op_type = op["o_type"]
                    op_status = op["status"]

                    # D操作显示ID和MD的具体状态
                    if op_type == "D":
                        id_status = "completed" if op.get("ID_completed", False) else (
                            "started" if op.get("ID_started", False) else "not started")
                        md_status = "completed" if op.get("MD_completed", False) else (
                            "started" if op.get("MD_started", False) else "not started")
                        print(f"        {op_id} (D): {op_status} [ID: {id_status}, MD: {md_status}]")
                    else:
                        print(f"        {op_id} ({op_type}): {op_status}")
        else:
            print(f"    作业队列: 空")

        # 2. 工作站资源状态
        print(f"    资源状态:")
        print(f"      ID资源: {ws.id_resource_busy}/{ws.id_resource} (忙碌/总数)")
        print(f"      MD资源: {ws.md_resource_busy}/{ws.md_resource} (忙碌/总数)")
        print(f"      3D打印机(类型{ws.printer_3d_type}): {ws.printer_3d_busy}/{ws.printer_3d} (忙碌/总数)")
        print(f"      当前加载材料: {ws.printer_material if ws.printer_material != 0 else '无'}")
        print(f"      装配资源: {ws.assembly_resource_busy}/{ws.assembly_resource} (忙碌/总数)")
        # print(f"    Cache区材料: {[f'材料{mat}:{count}' for mat, count in ws.cache_area.items() if count > 0]}")
        print(f"    Cache区材料: {[f'材料{mat}:{count}' for mat, count in ws.cache_area.items()]}")
        print(f"    WIP存储: {ws.temp_wip_storage if ws.temp_wip_storage else '空'}")


def print_order_execution_summary():
    """
    输出每个订单的执行情况汇总
    """
    print(f"\n{'=' * 120}")
    print("订单执行情况汇总")
    print(f"{'=' * 120}")
    # 表头
    print(
        f"{'订单ID':<10} | {'到达时间':<12} | {'Due Time':<12} | {'Release时间':<12} | {'完成时间':<12} | {'是否Tardy':<12} | {'Tardiness':<12}")
    print(f"{'-' * 10}-+-{'-' * 12}-+-{'-' * 12}-+-{'-' * 12}-+-{'-' * 12}-+-{'-' * 12}-+-{'-' * 12}")
    # 遍历所有订单
    for order in orders:
        order_id = order["order_id"]
        arrival_time = order["arrival_time"]
        due_time = order["due_time"]
        # 获取release时间
        release_time_str = f"{order_release_times[order_id]:.2f}" if order_id in order_release_times else "未释放"
        # 获取完成时间
        if order_id in order_completion_times:
            completion_time = order_completion_times[order_id]
            tardiness = max(0, completion_time - due_time)
            is_tardy = "是" if tardiness > 0 else "否"
            print(
                f"{order_id:<10} | {arrival_time:<12.2f} | {due_time:<12.2f} | {release_time_str:<12} | {completion_time:<12.2f} | {is_tardy:<12} | {tardiness:<12.2f}")
        else:
            # 订单未完成
            print(
                f"{order_id:<10} | {arrival_time:<12.2f} | {due_time:<12.2f} | {release_time_str:<12} | {'未完成':<12} | {'-':<12} | {'-':<12}")
    print(f"{'=' * 120}\n")

# endregion

# region 不确定性
#安排不确定性事件
def check_and_add_OU_event(order):
    """
    检查订单是否有OU事件，如果有则加入事件队列
    :param order: 订单对象
    """
    if "OU" in order and order["OU"] is not None:
        ou = order["OU"]
        # OU事件触发时间 = arrival_time + relative_time
        trigger_time = order["arrival_time"] + ou["relative_time"]
        # 创建OU事件
        ou_event = Event(
            event_type="uncertainty_OU",
            time=trigger_time,
            uncertainty_type="OU",
            order=order,
            ou_data=ou
        )
        event_queue.add_event(ou_event)
        print(f"  订单 {order['order_id']} 存在OU事件(type {ou['type']})，将在时间 {trigger_time} 触发")


def parse_EU_location(location):
    """
    解析EU事件的location，返回工作站和资源类型
    :param location: 位置字符串，如 "DM_3", "PM_2", "IDO_1"
    :return: (workstation对象, resource_type字符串)
    """
    parts = location.split('_')
    location_type = parts[0]
    location_num = int(parts[1])

    # 获取对应的工作站（location_num 1-6 对应 workstations[0-5]）
    ws = workstations[location_num - 1]

    # 映射到资源类型
    resource_mapping = {
        "DM": "id_resource",
        "PM": "printer_3d",
        "IDO": "id_resource",
        "MDO": "md_resource",
        "AO": "assembly_resource"
    }

    resource_type = resource_mapping.get(location_type)
    return ws, resource_type


def find_running_operation_on_resource(ws, resource_type):
    """
    查找工作站上正在使用指定资源的操作
    :param ws: 工作站对象
    :param resource_type: 资源类型
    :return: (job, operation) 如果找到，否则返回 (None, None)
    """
    for job in ws.job_queue:
        for operation in job["operations"]:
            if operation["status"] != "running":
                continue

            o_type = operation["o_type"]

            # 检查该操作是否使用了指定资源
            if resource_type == "id_resource" and o_type == "D":
                if operation.get("ID_started", False) and not operation.get("ID_completed", False):
                    return job, operation
            elif resource_type == "md_resource" and o_type == "D":
                if operation.get("MD_started", False) and not operation.get("MD_completed", False):
                    return job, operation
            elif resource_type == "printer_3d" and o_type == "P":
                return job, operation
            elif resource_type == "assembly_resource" and o_type == "A":
                return job, operation

    return None, None


def remove_operation_complete_event(job, operation):
    """
    从事件队列中移除指定操作的完成事件
    :param job: 作业对象
    :param operation: 操作对象
    """
    # 遍历事件队列，找到并移除对应的operation_complete事件
    events_to_remove = []
    for event in event_queue.queue:
        if (event.event_type == "operation_complete" and
                event.data.get("job") == job and
                event.data.get("operation") == operation):
            events_to_remove.append(event)

    for event in events_to_remove:
        event_queue.queue.remove(event)

    # 重新堆化
    import heapq
    heapq.heapify(event_queue.queue)

    if events_to_remove:
        print(f"    移除了 {len(events_to_remove)} 个operation_complete事件")


def remove_operation_JU_events(job, operation, exclude_ju=None):
    """
    从事件队列中移除指定操作的所有JU事件
    :param job: 作业对象
    :param operation: 操作对象
    :param exclude_ju: 要排除的JU对象（不移除该JU的事件）
    """
    # 遍历事件队列，找到并移除对应的JU事件
    events_to_remove = []
    for event in event_queue.queue:
        if event.event_type in ["uncertainty_JU", "uncertainty_start", "uncertainty_end"]:
            # 检查是否是该操作相关的JU事件
            event_job = event.data.get("job")
            event_operation = event.data.get("operation")
            event_ju = event.data.get("ju_data")
            uncertainty_type = event.data.get("uncertainty_type")

            if (event_job == job and event_operation == operation and
                    uncertainty_type == "JU"):
                # 如果指定了exclude_ju，则跳过该JU的事件
                if exclude_ju is not None and event_ju == exclude_ju:
                    continue
                events_to_remove.append(event)

    for event in events_to_remove:
        event_queue.queue.remove(event)

    # 重新堆化
    import heapq
    heapq.heapify(event_queue.queue)

    if events_to_remove:
        print(f"    移除了 {len(events_to_remove)} 个JU事件")


def handle_uncertainty_OU(event):
    """
    处理OU不确定性事件（订单级）
    :param event: OU事件对象
    """
    order = event.data["order"]
    ou_data = event.data["ou_data"]
    ou_type = ou_data["type"]
    # print(f"\nOU不确定性事件: Order {order['order_id']}, Type {ou_type} @ 时间 {current_time}")
    # 修改订单的due_time
    old_due_time = order["due_time"]
    new_due_time = ou_data["new_due_time"]
    order["due_time"] = new_due_time
    LS()


#处理持续类型事件开始（包括EU，JU8）
def handle_uncertainty_start(event):
    """
    处理不确定性事件开始
    :param event: 不确定性事件对象
    """
    uncertainty_type = event.data["uncertainty_type"]
    if uncertainty_type == "EU":
        # 处理EU事件开始
        eu_data = event.data["eu_data"]
        eu_type = eu_data["type"]
        location = eu_data["location"]
        print(f"\nEU不确定性事件开始: Type {eu_type}, 位置 {location} @ 时间 {current_time}")
        # 解析location，获取工作站和资源类型
        ws, resource_type = parse_EU_location(location)
        print(f"  影响工作站: {ws.ws_id}, 资源类型: {resource_type}")
        # 查找是否有操作正在使用该资源
        job, operation = find_running_operation_on_resource(ws, resource_type)
        if job and operation:
            # 有操作正在使用该资源，中断操作
            print(f"  中断操作: {operation['o_id']} (Job: {job['job_id']})")
            # 重置操作状态为ready
            operation["status"] = "ready"
            # 累加已执行的繁忙时间（执行部分计入，后续故障部分不计入）
            ws.accumulate_busy_time(resource_type)
            # 如果是D操作，需要重置对应的步骤标志
            if operation["o_type"] == "D":
                if operation.get("ID_started", False) and not operation.get("ID_completed", False):
                    operation["ID_started"] = False
                elif operation.get("MD_started", False) and not operation.get("MD_completed", False):
                    operation["MD_started"] = False
            # 从事件队列中移除该操作的完成事件
            remove_operation_complete_event(job, operation)
            # 从事件队列中移除该操作的所有JU事件
            remove_operation_JU_events(job, operation)
            # 资源不释放（保持busy状态，模拟资源不可用）
            print(f"  资源保持占用状态（模拟不可用）")
        else:
            # 没有操作使用该资源，占用资源以模拟不可用
            print(f"  资源当前空闲，占用资源以模拟不可用")
            if resource_type == "id_resource":
                ws.id_resource_busy += 1
            elif resource_type == "md_resource":
                ws.md_resource_busy += 1
            elif resource_type == "printer_3d":
                ws.printer_3d_busy += 1
            elif resource_type == "assembly_resource":
                ws.assembly_resource_busy += 1
    elif uncertainty_type == "JU":
        # 处理JU事件开始（仅Type 8持续型）
        job = event.data["job"]
        operation = event.data["operation"]
        ws = event.data["workstation"]
        ju_data = event.data["ju_data"]
        ju_type = ju_data["type"]
        if ju_type == 8:
            print(
                f"\nJU不确定性事件开始: Job {job['job_id']}, Operation {operation['o_id']}, Type 8 @ 时间 {current_time}")
            # 标记JU为已触发
            ju_data["status"] = "triggered"
            # D操作变为unready
            reset_operation_to_state(operation, "unready", {"ID"})
            print(f"  D操作设置为unready")
            # 释放ID资源
            ws.accumulate_busy_time("id_resource")  # 累加已执行的繁忙时间
            ws.id_resource_busy -= 1
            remove_operation_complete_event(job, operation)
            # 移除该操作的其他JU事件
            remove_operation_JU_events(job, operation, exclude_ju=ju_data)
            # 把作业从工作站局部队列移到BQ
            ws.pop_job(job)
            blocked_queues.append(job)
            print(f"  作业 {job['job_id']} 从 {ws.ws_id} 移到 BQ")
            # 创建OOOE事件让该工作站执行其他作业
            create_OOOE_event(ws)


#处理持续类型事件结束（包括EU，JU8）
def handle_uncertainty_end(event):
    """
    处理不确定性事件结束
    :param event: 不确定性事件对象
    """
    uncertainty_type = event.data["uncertainty_type"]
    if uncertainty_type == "EU":
        # 处理EU事件结束
        eu_data = event.data["eu_data"]
        eu_type = eu_data["type"]
        location = eu_data["location"]
        print(f"\nEU不确定性事件结束: Type {eu_type}, 位置 {location} @ 时间 {current_time}")
        # 解析location，获取工作站和资源类型
        ws, resource_type = parse_EU_location(location)
        print(f"  恢复工作站: {ws.ws_id}, 资源类型: {resource_type}")
        # 释放对应资源（accumulate安全：若是故障占用则busy_start为None不累加）
        ws.accumulate_busy_time(resource_type)
        if resource_type == "id_resource":
            ws.id_resource_busy -= 1
        elif resource_type == "md_resource":
            ws.md_resource_busy -= 1
        elif resource_type == "printer_3d":
            ws.printer_3d_busy -= 1
        elif resource_type == "assembly_resource":
            ws.assembly_resource_busy -= 1
        print(f"  资源已释放")
        # 新增：EU结束时触发SS，重新分配之前因资源不可用而延迟分配的job
        SS()
        # 创建该工作站的OOOE事件
        create_OOOE_event(ws)
    elif uncertainty_type == "JU":
        # 处理JU事件结束（仅Type 8持续型）
        job = event.data["job"]
        operation = event.data["operation"]
        ws = event.data["workstation"]
        ju_data = event.data["ju_data"]
        ju_type = ju_data["type"]
        if ju_type == 8:
            print(
                f"\nJU不确定性事件结束: Job {job['job_id']}, Operation {operation['o_id']}, Type 8 @ 时间 {current_time}")
            # ====================【CPUiS9 修改开始：顾客返回后原站 OoOE】====================
            # 修改：BQ 作业恢复原工作站局部就绪；目的：保留执行归属，避免重新 SS 分配及重复材料配送。
            if job in blocked_queues:
                blocked_queues.remove(job)
                operation["status"] = "ready"
                if not any(job in station.job_queue for station in workstations):
                    ws.add_job(job, current_time)
                print(f"  作业 {job['job_id']} 从 BQ 恢复到原工作站 {ws.ws_id}")
            elif job in blocked_suspended_queues:
                # 修改：BSQ 仅恢复至 RSQ；目的：保持挂起约束，仍由原有 MS 决定恢复。
                blocked_suspended_queues.remove(job)
                operation["status"] = "ready"
                if job not in ready_suspended_queues:
                    ready_suspended_queues.append(job)
                print(f"  作业 {job['job_id']} 从 BSQ 移到 RSQ")
            else:
                # 修改：忽略已处理的重复结束事件；目的：避免重置正在执行的操作或重复入队。
                return
            # 修改：不再调用 SS；先唤醒原站，再检查其他空闲扫描资源，让现有 WS 规则决定是否窃取。
            queued = {queued_event.data["workstation"].ws_id for queued_event in event_queue.queue
                      if queued_event.event_type == "OOOE" and queued_event.time == current_time}
            for station in [ws] + [candidate for candidate in workstations if candidate is not ws]:
                if station is ws or job in station.job_queue or station.id_resource > station.id_resource_busy:
                    if station.ws_id not in queued:
                        create_OOOE_event(station)
                        queued.add(station.ws_id)
            # ====================【CPUiS9 修改结束：保留其他同步策略】====================


def add_JU_events_for_operation(job, operation, ws, start_time, completion_time):
    """
    为操作添加JU不确定性事件到事件队列
    :param job: 作业对象
    :param operation: 操作对象
    :param ws: 工作站对象
    :param start_time: 操作开始时间
    :param completion_time: 操作预计完成时间
    """
    if "JU" not in job or not job["JU"]:
        return
    o_type = operation["o_type"]
    for ju in job["JU"]:
        if ju.get("status") != "untriggered":
            continue  # 已经触发过的JU不再添加
        ju_type = ju["type"]
        stage = ju.get("stage", "")
        # 判断该JU是否与当前操作相关
        trigger_time = None
        # Type 6: before_P or before_A
        if ju_type == 6:
            if stage == "before_P" and o_type == "P":
                trigger_time = start_time
        # Type 7: In_P
        elif ju_type in [7, 12, 13] and stage == "during_P" and o_type == "P":
            # 使用相对时间系数计算触发时间
            relative_time_coe = ju.get("relative_time_coe", 0)
            # 获取该P操作在当前工作站打印机类型下的实际运行时间
            machine_times = operation["machine_times"]
            actual_p_time = machine_times[str(ws.printer_3d_type)]["actual_time"]
            relative_time = relative_time_coe * actual_p_time
            trigger_time = start_time + relative_time
        # Type 8: before_ID
        elif ju_type == 8 and stage == "before_ID" and o_type == "D":
            # if not operation.get("ID_started", False):
            trigger_time = start_time
        # Type 9: during_ID
        elif ju_type == 9 and stage == "during_ID" and o_type == "D":
            if operation.get("ID_started", False):
                # ID刚开始执行
                trigger_time = start_time + ju.get("relative_time", 0)
        # Type 10: after_ID
        elif ju_type == 10 and stage == "after_ID" and o_type == "D":
            if operation.get("ID_started", False):
                # ID操作，在ID完成时触发
                id_completion_time = start_time
                trigger_time = id_completion_time
        # Type 11: after_MD
        elif ju_type == 11 and stage == "after_MD" and o_type == "D":
            if operation.get("ID_completed", False) and operation.get("MD_started", False):
                # MD刚开始执行，在MD完成时触发
                trigger_time = completion_time
        # # Type 12: during_P
        # elif ju_type == 12 and stage == "during_P" and o_type == "P":
        #     trigger_time = start_time + ju.get("relative_time", 0)
        # # Type 13: during_P
        # elif ju_type == 13 and stage == "during_P" and o_type == "P":
        #     trigger_time = start_time + ju.get("relative_time", 0)
        # Type 14: after_P
        elif ju_type == 14 and stage == "after_P" and o_type == "P":
            trigger_time = completion_time
        # Type 15: during_A
        elif ju_type == 15 and stage == "during_A" and o_type == "A":
            trigger_time = start_time + ju.get("relative_time", 0)
        # Type 16: after_A
        elif ju_type == 16 and stage == "after_A" and o_type == "A":
            trigger_time = completion_time
        # 如果确定了触发时间，创建JU事件
        if trigger_time is not None:
            if ju_type == 8:
                # Type 8是持续型事件，创建开始和结束事件
                start_event = Event(
                    event_type="uncertainty_start",
                    time=trigger_time,
                    uncertainty_type="JU",
                    job=job,
                    operation=operation,
                    workstation=ws,
                    ju_data=ju
                )
                event_queue.add_event(start_event)
                end_event = Event(
                    event_type="uncertainty_end",
                    time=trigger_time + ju["duration"],
                    uncertainty_type="JU",
                    job=job,
                    operation=operation,
                    workstation=ws,
                    ju_data=ju
                )
                event_queue.add_event(end_event)
                print(
                    f"  添加JU事件 Type {ju_type} (持续型): 开始时间 {trigger_time}, 结束时间 {trigger_time + ju['duration']}")
            else:
                # 瞬时型事件
                ju_event = Event(
                    event_type="uncertainty_JU",
                    time=trigger_time,
                    uncertainty_type="JU",
                    job=job,
                    operation=operation,
                    workstation=ws,
                    ju_data=ju
                )
                # event_queue.add_event(ju_event)
                event_queue.add_event_front(ju_event)
                print(f"  添加JU事件 Type {ju_type}: 触发时间 {trigger_time}")


def reset_operation_to_state(operation, target_state, reset_details=None):
    """
    重置操作状态
    :param operation: 操作对象
    :param target_state: 目标状态 ("ready" or "unready")
    :param reset_details: 重置详情字典，指定需要重置的内容
    """
    o_type = operation["o_type"]
    if o_type == "D":
        if reset_details and "ID" in reset_details:
            operation["ID_started"] = False
            operation["ID_completed"] = False
            print(f"      重置ID步骤")
        if reset_details and "MD" in reset_details:
            operation["MD_started"] = False
            operation["MD_completed"] = False
            print(f"      重置MD步骤")
    operation["status"] = target_state
    print(f"      操作状态设置为: {target_state}")


def handle_uncertainty_JU(event):
    """
    处理JU不确定性事件（作业级，瞬时型）
    :param event: JU事件对象
    """
    job = event.data["job"]
    operation = event.data["operation"]
    ws = event.data["workstation"]
    ju_data = event.data["ju_data"]
    ju_type = ju_data["type"]
    print(f"\nJU不确定性事件: Job {job['job_id']}, Operation {operation['o_id']}, Type {ju_type} @ 时间 {current_time}")
    # 标记JU为已触发
    ju_data["status"] = "triggered"
    # 移除该操作的operation_complete事件（如果存在）
    remove_operation_complete_event(job, operation)
    # 移除该操作的其他JU事件（排除当前JU）
    remove_operation_JU_events(job, operation, exclude_ju=ju_data)
    # 根据JU类型进行处理
    if ju_type == 6:
        # Type 6: P操作中断，材料耗尽
        print(f"  Type 6: P操作中断，材料耗尽")
        material_type = operation["material"]
        # 材料耗尽
        ws.cache_area[material_type] -= 1
        ws.printer_material = 0
        print(f"    工作站 {ws.ws_id} Cache材料{material_type} -1，打印机材料清空")
        # 释放打印机资源
        ws.accumulate_busy_time("printer_3d")
        ws.printer_3d_busy -= 1
        # 重置操作为ready
        operation["status"] = "ready"
        # 创建物流事件：memory/harddisk -> cache 运送材料
        if memory_storage[material_type] > 0:
            create_logistics_event_memory_cache_transfer(ws, material_type, 1)
        else:
            create_logistics_event_harddisk_cache_transfer(ws, material_type, 1)
        # 创建OOOE事件
        create_OOOE_event(ws)
    elif ju_type == 7:
        # Type 7: P操作中断，load材料清空
        print(f"  Type 7: P操作中断，load材料清空")
        ws.printer_material = 0
        print(f"    工作站 {ws.ws_id} 打印机材料清空")
        # 释放打印机资源
        ws.accumulate_busy_time("printer_3d")
        ws.printer_3d_busy -= 1
        # 重置操作为ready
        operation["status"] = "ready"
        # 创建OOOE事件
        create_OOOE_event(ws)
    elif ju_type == 9:
        # Type 9: ID中，重置ID
        print(f"  Type 9: 重置ID步骤")
        # 释放ID资源
        ws.accumulate_busy_time("id_resource")
        ws.id_resource_busy -= 1
        # 重置ID
        reset_operation_to_state(operation, "ready", {"ID"})
        # 创建OOOE事件
        create_OOOE_event(ws)
    elif ju_type == 10:
        # Type 10: ID后，重置ID
        print(f"  Type 10: 重置ID步骤")
        # 释放ID资源
        ws.accumulate_busy_time("id_resource")
        ws.id_resource_busy -= 1
        # 重置ID
        reset_operation_to_state(operation, "ready", {"ID"})
        # 创建OOOE事件
        create_OOOE_event(ws)
    elif ju_type == 11:
        # Type 11: MD后，重置MD（ID保持完成）
        print(f"  Type 11: 重置MD步骤")
        # 释放MD资源
        ws.accumulate_busy_time("md_resource")
        ws.md_resource_busy -= 1
        # 重置MD，保持ID完成
        reset_operation_to_state(operation, "ready", {"MD"})
        # 创建OOOE事件
        create_OOOE_event(ws)
    elif ju_type == 12:
        # Type 12: P中，重置P和MD
        print(f"  Type 12: 重置P操作和MD步骤")
        # 释放打印机资源
        ws.accumulate_busy_time("printer_3d")
        ws.printer_3d_busy -= 1
        # 重置P操作为unready
        operation["status"] = "unready"
        # 找到该job的D操作，重置MD
        for op in job["operations"]:
            if op["o_type"] == "D":
                reset_operation_to_state(op, "ready", {"MD"})
                break
        # 创建OOOE事件
        create_OOOE_event(ws)
    elif ju_type == 13:
        # Type 13: P中，重置P
        print(f"  Type 13: 重置P操作")
        # 释放打印机资源
        ws.accumulate_busy_time("printer_3d")
        ws.printer_3d_busy -= 1
        # 重置P操作为ready
        operation["status"] = "ready"
        # 创建OOOE事件
        create_OOOE_event(ws)
    elif ju_type == 14:
        # Type 14: P后，重置P
        print(f"  Type 14: 重置P操作")
        # 释放打印机资源
        ws.accumulate_busy_time("printer_3d")
        ws.printer_3d_busy -= 1
        # 重置P操作为ready
        operation["status"] = "ready"
        # 创建OOOE事件
        create_OOOE_event(ws)
    elif ju_type == 15:
        # Type 15: A中，重置A
        print(f"  Type 15: 重置A操作")
        # 释放装配资源
        ws.accumulate_busy_time("assembly_resource")
        ws.assembly_resource_busy -= 1
        # 重置A操作为ready
        operation["status"] = "ready"
        # 创建OOOE事件
        create_OOOE_event(ws)
    elif ju_type == 16:
        # Type 16: A后，重置A
        print(f"  Type 16: 重置A操作")
        # 释放装配资源
        ws.accumulate_busy_time("assembly_resource")
        ws.assembly_resource_busy -= 1
        # 重置A操作为ready
        operation["status"] = "ready"
        # 创建OOOE事件
        create_OOOE_event(ws)


# endregion


# region 结果输出
#计算结果指标
def calculate_metrics():
    """
    计算并输出仿真结果指标
    """
    print(f"\n{'=' * 80}")
    print("仿真结果指标")
    print(f"{'=' * 80}")

    # 1. Makespan (总完成时间)
    if order_completion_times:
        makespan = max(order_completion_times.values())
    else:
        makespan = current_time
    print(f"\n1. Makespan (总完成时间): {makespan:.2f}")

    # 2. Mean Setup Time (平均准备时间)
    # 计算总的P操作数量
    total_p_operations = 0
    for order in orders:
        for job in order["jobs"]:
            for operation in job["operations"]:
                if operation["o_type"] == "P":
                    total_p_operations += 1

    if total_p_operations > 0:
        mean_setup_time = total_setup_time / total_p_operations
        print(f"\n2. Mean Setup Time (平均材料切换时间): {mean_setup_time:.2f}")
        print(f"   总Setup Time: {total_setup_time:.2f}")
        print(f"   P操作总数: {total_p_operations}")
    else:
        print(f"\n2. Mean Setup Time: N/A (没有P操作)")

    # 3. Mean Job Flow Time (平均作业流程时间)
    total_job_flow_time = 0
    job_count = 0

    for order in orders:
        order_arrival_time = order["arrival_time"]
        for job in order["jobs"]:
            job_id = job["job_id"]
            if job_id in job_completion_times:
                job_flow_time = job_completion_times[job_id] - order_arrival_time
                total_job_flow_time += job_flow_time
                job_count += 1

    if job_count > 0:
        mean_job_flow_time = total_job_flow_time / job_count
        print(f"\n3. Mean Job Flow Time (平均作业流程时间): {mean_job_flow_time:.2f}")
        print(f"   完成的Job数量: {job_count}")
    else:
        print(f"\n3. Mean Job Flow Time: N/A (没有完成的Job)")

    # 4. Mean Order Flow Time (平均订单流程时间)
    total_order_flow_time = 0
    order_count = 0

    for order in orders:
        order_id = order["order_id"]
        if order_id in order_completion_times:
            order_flow_time = order_completion_times[order_id] - order["arrival_time"]
            total_order_flow_time += order_flow_time
            order_count += 1

    if order_count > 0:
        mean_order_flow_time = total_order_flow_time / order_count
        print(f"\n4. Mean Order Flow Time (平均订单流程时间): {mean_order_flow_time:.2f}")
        print(f"   完成的Order数量: {order_count}")
    else:
        print(f"\n4. Mean Order Flow Time: N/A (没有完成的Order)")

    # 5. Mean Order Tardiness (平均订单延迟时间)
    total_tardiness = 0
    max_tardiness = 0
    num_tardy_orders = 0

    for order in orders:
        order_id = order["order_id"]
        if order_id in order_completion_times:
            completion_time = order_completion_times[order_id]
            due_time = order["due_time"]
            tardiness = max(0, completion_time - due_time)

            total_tardiness += tardiness
            max_tardiness = max(max_tardiness, tardiness)

            if tardiness > 0:
                num_tardy_orders += 1

    # 延迟订单按"初始订单(arrival=0) / 后续到达订单(arrival>0)"拆分统计
    initial_total = 0
    initial_tardy = 0
    subsequent_total = 0
    subsequent_tardy = 0
    for order in orders:
        order_id = order["order_id"]
        is_initial = (order["arrival_time"] == 0)
        if is_initial:
            initial_total += 1
        else:
            subsequent_total += 1
        if order_id in order_completion_times:
            completion_time = order_completion_times[order_id]
            due_time = order["due_time"]  # 调整后due_time，与num_tardy_orders口径一致
            if completion_time - due_time > 0:
                if is_initial:
                    initial_tardy += 1
                else:
                    subsequent_tardy += 1

    if order_count > 0:
        mean_tardiness = total_tardiness / order_count
        print(f"\n5. Mean Order Tardiness (平均订单延迟时间): {mean_tardiness:.2f}")
        print(f"   总延迟时间: {total_tardiness:.2f}")
        print(f"   最大延迟时间: {max_tardiness:.2f}")
    else:
        print(f"\n5. Mean Order Tardiness: N/A")

    # 6. Number of Tardy Orders (延迟订单数量)
    print(f"\n6. Number of Tardy Orders (延迟订单统计):")
    print(f"   延迟订单数量: {num_tardy_orders}")
    print(f"   按时订单数量: {order_count - num_tardy_orders}")
    if order_count > 0:
        tardy_rate = (num_tardy_orders / order_count) * 100
        print(f"   延迟率: {tardy_rate:.2f}%")

    # 7. Workload Balance (CV 和 Range)
    cell_utilizations = []
    for ws in workstations:
        total_busy = sum(ws.busy_time.values())
        num_resources = 4  # id, md, printer, assembly
        if makespan > 0:
            U_c = total_busy / (num_resources * makespan)
        else:
            U_c = 0
        cell_utilizations.append(U_c)

    N = len(cell_utilizations)
    U_mean = sum(cell_utilizations) / N if N > 0 else 0

    # CV（变异系数）
    if U_mean == 0:
        cv = 0  # 边界情况
    else:
        variance = sum((u - U_mean) ** 2 for u in cell_utilizations) / N
        sigma = variance ** 0.5
        cv = sigma / U_mean

    # Range（极差）
    utilization_range = (max(cell_utilizations) - min(cell_utilizations)) if cell_utilizations else 0

    print(f"\n7. Workload Balance (负载均衡):")
    print(f"   各cell利用率: {[f'{u:.3f}' for u in cell_utilizations]}")
    print(f"   平均利用率 U_mean: {U_mean:.4f}")
    print(f"   CV (变异系数): {cv:.4f}")
    print(f"   Range (极差): {utilization_range:.4f}")

    print(f"\n{'=' * 80}")

    print(f"\n{'=' * 80}")
    return makespan, mean_setup_time, mean_job_flow_time, mean_order_flow_time, mean_tardiness, num_tardy_orders, cv, utilization_range, initial_tardy, initial_total, subsequent_tardy, subsequent_total
# endregion


#调用
def run_simulation(orders_file, seed):
    """
    运行单次仿真的包装函数
    参数：
        orders_file: 订单文件路径
        seed: 随机种子
    返回：
        metrics字典，包含6个指标
    """
    random.seed(seed)
    # 重置所有全局变量
    global current_time, orders, EU_events, workstations, event_queue
    global unreleased_orders, released_orders, suspended_orders, completed_orders
    global global_ready_queues, ready_suspended_queues, blocked_suspended_queues, blocked_queues
    global total_setup_time, job_start_times, job_completion_times, order_completion_times, order_release_times
    global memory_storage

    # 重新读取订单数据
    data = order_generate5.read_orders(orders_file)
    orders = data["orders"]
    EU_events = data.get("EU_events", [])

    # 为所有JU事件初始化status
    for order in orders:
        for job in order["jobs"]:
            if "JU" in job and job["JU"]:
                for ju in job["JU"]:
                    ju["status"] = "untriggered"

    # 重置全局变量
    current_time = 0

    # 重置订单队列
    unreleased_orders = []
    released_orders = []
    suspended_orders = []
    completed_orders = []

    # 重置作业队列
    global_ready_queues = []
    ready_suspended_queues = []
    blocked_suspended_queues = []
    blocked_queues = []

    # 重置统计变量
    total_setup_time = 0
    job_start_times = {}
    job_completion_times = {}
    order_completion_times = {}
    order_release_times = {}

    # 重置内存存储
    memory_storage = {material_type: 1 for material_type in MATERIAL_TYPES}

    # 重新创建工作站
    workstations = []
    printer_types = [1, 1, 1, 1, 1, 1]
    for i in range(TOTAL_WORKSTATIONS):
        ws_id = f"WS-{i + 1:02d}"
        printer_type = printer_types[i]
        station = WorkStation(ws_id, printer_type)
        mapping = {
            0: [1, 2, 3],
            1: [4, 5, 6],
            2: [7, 8, 9],
            3: [10, 1, 2],
            4: [3, 4, 5],
            5: [6, 7, 8],
        }
        for idx in mapping.get(i, []):
            station.cache_area[idx] = 1
        workstations.append(station)

    # 重置事件队列
    event_queue = EventQueue()

    # 运行仿真
    run()

    # 获取结果
    makespan, mean_setup_time, mean_job_flow_time, mean_order_flow_time, mean_tardiness, num_tardy_orders, cv, utilization_range, initial_tardy, initial_total, subsequent_tardy, subsequent_total = calculate_metrics()

    return {
        "makespan": makespan,
        "mean_setup_time": mean_setup_time,
        "mean_job_flow_time": mean_job_flow_time,
        "mean_order_flow_time": mean_order_flow_time,
        "mean_tardiness": mean_tardiness,
        "num_tardy_orders": num_tardy_orders,
        "cv": cv,
        "utilization_range": utilization_range,
        "initial_tardy": initial_tardy,
        "initial_total": initial_total,
        "subsequent_tardy": subsequent_tardy,
        "subsequent_total": subsequent_total
    }


# ==================== 使用示例 ====================
if __name__ == "__main__":
    # 运行仿真
    run()

    # 显示最终状态
    # print("\n最终工作站状态:")
    # for ws in workstations:
    #     ws.display_status()

    # display_memory_status()
    print(f"全局就绪队列：{global_ready_queues}")

    # print("wtf")
    # print()
    # metrics = run_simulation(orders_file="new_orders.json", seed=42)
    # # metrics = run_simulation(orders_file="orders_50.json", seed=42)
    # print(f"✓ (Makespan: {metrics['makespan']:.2f})")