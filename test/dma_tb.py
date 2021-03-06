#!/usr/bin/env python3
import random

from litex.gen import *
from litex.soc.interconnect import stream
from litex.soc.interconnect.stream_sim import *

from litepcie.common import *
from litepcie.core import LitePCIeEndpoint
from litepcie.core.msi import LitePCIeMSI
from litepcie.frontend.dma import LitePCIeDMAWriter, LitePCIeDMAReader

from model.host import *

DMA_READER_IRQ = 1
DMA_WRITER_IRQ = 2

root_id = 0x100
endpoint_id = 0x400
max_length = Signal(8, reset=128)
dma_size = 1024


class DMADriver():
    def __init__(self, dma, dut):
        self.dma = getattr(dut, dma)
        self.dut = dut

    def set_prog_mode(self):
        yield self.dma.table._loop_prog_n.storage.eq(0)
        yield

    def set_loop_mode(self):
        yield self.dma.table._loop_prog_n.storage.eq(1)
        yield

    def flush(self):
        yield self.dma.table._flush.re.eq(1)
        yield
        yield self.dma.table._flush.re.eq(0)
        yield

    def program_descriptor(self, address, length):
        value = address
        value |= (length << 32)

        yield self.dma.table._value.storage.eq(value)
        yield self.dma.table._we.r.eq(1)
        yield self.dma.table._we.re.eq(1)
        yield
        yield self.dma.table._we.re.eq(0)
        yield

    def enable(self):
        yield self.dma._enable.storage.eq(1)
        yield

    def disable(self):
        yield self.dma._enable.storage.eq(0)
        yield


class InterruptHandler(Module):
    def __init__(self, debug=False):
        self.debug = debug
        self.sink = stream.Endpoint(interrupt_layout())

        self.dma_reader_irq_count = 0
        self.dma_writer_irq_count = 0

    def clear_dma_reader_irq_count(self):
        self.dma_writer_irq_count = 0

    def clear_dma_writer_irq_count(self):
        self.dma_writer_irq_count = 0

    @passive
    def generator(self, dut):
        last_valid = 0
        while True:
            yield dut.msi._clear.r.eq(0)
            yield dut.msi._clear.re.eq(0)
            yield self.sink.ready.eq(1)
            if (yield self.sink.valid) and not last_valid:
                # get vector
                irq_vector = (yield dut.msi._vector.status)

                # handle irq
                if irq_vector & DMA_READER_IRQ:
                    self.dma_reader_irq_count += 1
                    if self.debug:
                        print("DMA_READER IRQ, count: {:d}".format(self.dma_reader_irq_count))
                    # clear msi
                    yield dut.msi._clear.re.eq(1)
                    yield dut.msi._clear.r.eq((yield dut.msi._clear.r) | DMA_READER_IRQ)

                if irq_vector & DMA_WRITER_IRQ:
                    self.dma_writer_irq_count += 1
                    if self.debug:
                        print("DMA_WRITER IRQ, count: {:d}".format(self.dma_writer_irq_count))
                    # clear msi
                    yield dut.msi._clear.re.eq(1)
                    yield dut.msi._clear.r.eq((yield dut.msi._clear.r) | DMA_WRITER_IRQ)
            last_valid = (yield self.sink.valid)
            yield


test_size = 4*1024


class TB(Module):
    def __init__(self, with_converter=False):
        self.submodules.host = Host(64, root_id, endpoint_id,
            phy_debug=False,
            chipset_debug=False, chipset_split=True, chipset_reordering=True,
            host_debug=True)
        self.submodules.endpoint = LitePCIeEndpoint(self.host.phy, max_pending_requests=8, with_reordering=True)
        self.submodules.dma_reader = LitePCIeDMAReader(self.endpoint, self.endpoint.crossbar.get_master_port(read_only=True))
        self.submodules.dma_writer = LitePCIeDMAWriter(self.endpoint, self.endpoint.crossbar.get_master_port(write_only=True))

        if with_converter:
                self.submodules.up_converter = stream.StrideConverter(dma_layout(16), dma_layout(64))
                self.submodules.down_converter = stream.StrideConverter(dma_layout(64), dma_layout(16))
                self.submodules += stream.Pipeline(self.dma_reader,
                                                   self.down_converter,
                                                   self.up_converter,
                                                   self.dma_writer)
        else:
            self.comb += self.dma_reader.source.connect(self.dma_writer.sink)

        self.submodules.msi = LitePCIeMSI(2)
        self.comb += [
            self.msi.irqs[log2_int(DMA_READER_IRQ)].eq(self.dma_reader.irq),
            self.msi.irqs[log2_int(DMA_WRITER_IRQ)].eq(self.dma_writer.irq)
        ]
        self.submodules.irq_handler = InterruptHandler(debug=False)
        self.comb += self.msi.source.connect(self.irq_handler.sink)

def main_generator(dut):
    dut.host.malloc(0x00000000, test_size*2)
    dut.host.chipset.enable()
    host_datas = [seed_to_data(i, True) for i in range(test_size//4)]
    dut.host.write_mem(0x00000000, host_datas)

    dma_reader_driver = DMADriver("dma_reader", dut)
    dma_writer_driver = DMADriver("dma_writer", dut)

    yield from dma_reader_driver.set_prog_mode()
    yield from dma_reader_driver.flush()
    for i in range(8):
        yield from dma_reader_driver.program_descriptor((test_size//8)*i, test_size//8)

    yield from dma_writer_driver.set_prog_mode()
    yield from dma_writer_driver.flush()
    for i in range(8):
        yield from dma_writer_driver.program_descriptor(test_size + (test_size//8)*i, test_size//8)

    yield dut.msi._enable.storage.eq(DMA_READER_IRQ | DMA_WRITER_IRQ)

    yield from dma_reader_driver.enable()
    yield from dma_writer_driver.enable()

    while dut.irq_handler.dma_writer_irq_count != 8:
        yield

    for i in range(1000):
        yield

    loopback_datas = dut.host.read_mem(test_size, test_size)

    s, l, e = check(host_datas, loopback_datas)
    print("shift " + str(s) + " / length " + str(l) + " / errors " + str(e))

if __name__ == "__main__":
    tb = TB()
    generators = {
        "sys" :   [main_generator(tb),
                   tb.irq_handler.generator(tb),
                   tb.host.generator(),
                   tb.host.chipset.generator(),
                   tb.host.phy.phy_sink.generator(),
                   tb.host.phy.phy_source.generator()]
    }
    clocks = {"sys": 10}
    run_simulation(tb, generators, clocks, vcd_name="sim.vcd")
