
from GridCal.Engine.Core.multi_circuit import MultiCircuit
from GridCal.Engine.Devices.branch import BranchType
from GridCal.Engine.Devices.bus import Bus

from networkx import DiGraph, all_simple_paths
import numpy as np
import pandas as pd
from scipy.sparse import lil_matrix, csc_matrix
from PySide2.QtCore import QThread, QRunnable, Signal
from typing import List

pd.set_option('display.max_rows', 500)
pd.set_option('display.max_columns', 500)
pd.set_option('display.width', 1000)


def get_branches_of_bus(B, j):
    """
    Get the indices of the branches connected to the bus j
    :param B: Branch-bus CSC matrix
    :param j: bus index
    :return: list of branches in the bus
    """
    return [B.indices[k] for k in range(B.indptr[j], B.indptr[j + 1])]


def select_branches_to_reduce(circuit: MultiCircuit, rx_criteria=True, rx_threshold=1e-5,
                              selected_types=BranchType.Branch):
    """
    Find branches to remove
    Args:
        circuit: Circuit to modify in-place
        rx_criteria: use the r+x threshold to select branches?
        rx_threshold: r+x threshold
        selected_types: branch types to select
    """

    branches_to_remove_idx = list()

    for i in range(len(circuit.branches)):

        # is this branch of the selected type?
        if circuit.branches[i].branch_type in selected_types:

            # Am I filtering by r+x threshold?
            if rx_criteria:

                # compute the r+x ratio
                rx = circuit.branches[i].R + circuit.branches[i].X

                # if the r+x criteria is met, add it
                if rx < rx_threshold:
                    print(i, '->', rx, '<', rx_threshold)
                    branches_to_remove_idx.append(i)

            else:
                # Add the branch because it was selected and there is no further criteria
                branches_to_remove_idx.append(i)

    return branches_to_remove_idx


def reduce_grid_brute(circuit: MultiCircuit, removed_br_idx):
    """
    Remove the first branch found to be removed.
    this function is meant to be called until it returns false
    Args:
        circuit: Circuit to modify in-place
        removed_br_idx: branch index

    Returns: Nothing
    """

    # form C
    m = len(circuit.branches)
    n = len(circuit.buses)
    buses_dict = {bus: i for i, bus in enumerate(circuit.buses)}
    C = lil_matrix((m, n), dtype=int)
    graph = DiGraph()

    # TODO: Fix the topology reduction with the GC example, see what is going on

    for i in range(len(circuit.branches)):
        # get the from and to bus indices
        f = buses_dict[circuit.branches[i].bus_from]
        t = buses_dict[circuit.branches[i].bus_to]
        graph.add_edge(f, t)
        C[i, f] = 1
        C[i, t] = -1

    C = csc_matrix(C)

    # get branch buses
    bus_f = circuit.branches[removed_br_idx].bus_from
    bus_t = circuit.branches[removed_br_idx].bus_to
    f = buses_dict[bus_f]
    t = buses_dict[bus_t]

    removed_bus = None
    removed_branch = None
    updated_bus = None
    updated_branches = list()

    # get the number of paths
    n_paths = len(list(all_simple_paths(graph, f, t)))

    # print('Deleting: ', circuit.branches[br_idx].name)

    if n_paths == 1:

        # get the branches that are connected to the bus f
        adjacent_br_idx = get_branches_of_bus(C, f)

        for k in adjacent_br_idx:

            # get the indices of the buses
            f2 = buses_dict[circuit.branches[k].bus_from]
            t2 = buses_dict[circuit.branches[k].bus_to]

            # re-assign the right bus
            if f2 == f:
                circuit.branches[k].bus_from = bus_t
            elif t2 == t2:
                circuit.branches[k].bus_to = bus_t

            # copy the state of the removed branch
            circuit.branches[k].active = circuit.branches[removed_br_idx].active

            # remember the updated branches
            updated_branches.append(circuit.branches[k])

        # merge buses
        bus_t.merge(bus_f)
        updated_bus = bus_t

        # delete bus
        removed_bus = circuit.buses.pop(f)

        # remove the branch and that's it
        removed_branch = circuit.branches.pop(removed_br_idx)

    else:
        # remove the branch and that's it
        removed_branch = circuit.branches.pop(removed_br_idx)

    # return the removed branch and the possible removed bus
    return removed_branch, removed_bus, updated_bus, updated_branches


def reduce_buses(circuit: MultiCircuit, buses_to_reduce: List[Bus]):
    """
    Reduce the uses in the grid
    This function removes the buses but whenever a bus is removed, the devices connected to it
    are inherited by the bus of higher voltage that is connected.
    If the bus is isolated, those devices are lost.
    :param circuit: MultiCircuit instance
    :param buses_to_reduce: list of Bus objects
    :return: Nothing
    """

    # create dictionary of bus relationships
    bus_bus = dict()
    for branch in circuit.branches:
        f = branch.bus_from
        t = branch.bus_to

        # add that "t" is related to "f"
        if f in bus_bus.keys():
            bus_bus[f].append(t)
        else:
            bus_bus[f] = [t]

        # add that "f" is related to "t"
        if t in bus_bus.keys():
            bus_bus[t].append(f)
        else:
            bus_bus[t] = [f]

    # sort on voltage
    for bus, related in bus_bus.items():
        related.sort(key=lambda x: x.Vnom, reverse=True)

    buses_merged = list()

    # remove
    for bus in buses_to_reduce:

        if bus in bus_bus.keys():
            related_buses = bus_bus[bus]

            if len(related_buses) > 0:
                selected = related_buses.pop(0)
                while selected not in circuit.buses and len(related_buses) > 0:
                    selected = related_buses.pop(0)

                # merge the bus with the selected one
                print('Assigning', bus.name, 'to', selected.name)
                selected.merge(bus)

                # merge the graphics
                if selected.graphic_obj is not None and bus.graphic_obj is not None:
                    selected.graphic_obj.merge(bus.graphic_obj)

                # remember the buses that keep the devices
                buses_merged.append(selected)

                # delete the bus from the circuit and the dictionary
                circuit.delete_bus(bus)
                bus_bus.__delitem__(bus)
            else:
                # the bus is isolated, so delete it
                circuit.delete_bus(bus)

        else:
            # the bus is isolated, so delete it
            circuit.delete_bus(bus)

    return buses_merged

class TopologyReductionOptions:

    def __init__(self, rx_criteria=False, rx_threshold=1e-5, selected_types=BranchType.Branch):
        """
        Topology reduction options
        :param rx_criteria:
        :param rx_threshold:
        :param selected_types:
        """

        self.rx_criteria = rx_criteria
        self.rx_threshold = rx_threshold
        self.selected_type = selected_types


class TopologyReduction(QThread):
    progress_signal = Signal(float)
    progress_text = Signal(str)
    done_signal = Signal()

    def __init__(self, grid: MultiCircuit, branch_indices):
        """
        Topology reduction driver
        :param grid: MultiCircuit instance
        :param options:
        """
        QThread.__init__(self)

        self.grid = grid

        self.br_to_remove = branch_indices

        self.__cancel__ = False

    def run(self):
        """
        Run the monte carlo simulation
        @return:
        """
        self.progress_signal.emit(0.0)
        self.progress_text.emit('Detecting which branches to remove...')

        # sort the branches in reverse order
        self.br_to_remove.sort(reverse=True)

        total = len(self.br_to_remove)

        # for every branch in reverse order...
        for i, br_idx in enumerate(self.br_to_remove):

            # delete branch
            removed_branch, removed_bus, \
            updated_bus, updated_branches = reduce_grid_brute(circuit=self.grid, removed_br_idx=br_idx)

            # display progress
            self.progress_text.emit('Removed branch ' + str(br_idx) + ': ' + removed_branch.name)
            progress = (i+1) / total * 100
            self.progress_signal.emit(progress)

        # display progress
        self.progress_text.emit('Done')
        self.progress_signal.emit(0.0)
        self.done_signal.emit()

    def cancel(self):
        """
        Cancel the simulation
        :return:
        """
        self.__cancel__ = True
        self.progress_signal.emit(0.0)
        self.progress_text.emit('Cancelled')
        self.done_signal.emit()


if __name__ == '__main__':
    from matplotlib import pyplot as plt
    # fname = 'D:\\GitHub\\GridCal\\Grids_and_profiles\\grids\\Reduction Model 3.xlsx'
    fname = 'D:\\GitHub\\GridCal\\UnderDevelopment\\GridCal\\Engine\\IO\\Export_sensible_v15_modif.json.xlsx'

    circuit_ = MultiCircuit()
    circuit_.load_file(fname)
    # circuit.compile()
    top = TopologyReduction(grid=circuit_, rx_criteria=False, rx_threshold=1e-5,
                            type_criteria=True, selected_type=BranchType.Branch)
    top.run()
    # circuit_.compile()
    # circuit_.plot_graph()
    # plt.show()