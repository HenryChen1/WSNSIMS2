import collections
import itertools
import logging
from copy import deepcopy

import numpy as np

import scipy.sparse.csgraph as sp
from PIL._util import deferred_error

from wsnsims.core.data import segment_volume

logger = logging.getLogger(__name__)


class LOAFEnergyModelError(Exception):
    pass


class LOAFEnergyModel(object):
    def __init__(self, simulation_data, environment):
        """

        :param simulation_data:
        :type simulation_data: loaf.loaf_sim.LOAF
        :param environment:
        :type environment: core.environment.Environment
        """

        self.sim = simulation_data
        self.env = environment
        self.cluster_graph = self.build_cluster_graph()

        self._ids_to_clusters = {}
        self._ids_to_movement_energy = {}
        self._ids_to_comms_energy = {}

    def build_cluster_graph(self):

        cluster_graph = collections.defaultdict(list)
        for cluster in self.sim.clusters:

            other_clusters = list(self.sim.clusters)
            other_clusters.remove(cluster)
            for other_cluster in other_clusters:
                overlaps = set(cluster.tour.objects).intersection(
                    other_cluster.tour.objects)

                if len(overlaps) > 0:
                    cluster_graph[cluster].append(other_cluster)

        node_count = len(self.sim.clusters)
        dense = np.zeros((node_count, node_count), dtype=float)

        for cluster, neighbors in cluster_graph.items():

            cluster_index = self.sim.clusters.index(cluster)
            for neighbor in neighbors:
                neighbor_index = self.sim.clusters.index(neighbor)

                dense[cluster_index, neighbor_index] = 1
                dense[neighbor_index, cluster_index] = 1

        sparse = sp.csgraph_from_dense(dense)
        return sparse

    def cluster_data_volume(self, cluster_id, intercluster_only=False):
        """

        :param cluster_id:
        :param intercluster_only:
        :return:
        :rtype: pq.bit
        """

        current_cluster = self._find_cluster(cluster_id)

        cluster_index = self.sim.clusters.index(current_cluster)

        # Remove the Pi from the cluster before calculating the data volumes
        current_cluster = deepcopy(current_cluster)
        for segment in list(current_cluster.nodes):
            if hasattr(segment, "fake_segment"):
                current_cluster.remove(segment)

        cluster_tree, preds = sp.breadth_first_order(self.cluster_graph,
                                                     cluster_index,
                                                     directed=False,
                                                     return_predecessors=True)

        children_indexes = list()
        for index in cluster_tree:
            if preds[index] == cluster_index:
                children_indexes.append(index)

        cluster_graph = self.cluster_graph.toarray()
        cluster_graph[cluster_index] = 0
        cluster_graph[:, cluster_index] = 0

        components_count, labels = sp.connected_components(cluster_graph,
                                                           directed=False)

        cluster_groups = collections.defaultdict(list)
        for index, label in enumerate(labels):
            if index == cluster_index:
                continue

            cluster_groups[label].append(self.sim.clusters[index])

        # Build a set of "super clusters." These are just collections of all
        # segments from the clusters that make up each child branch from the
        # current cluster.
        super_clusters = collections.defaultdict(list)
        for label, clusters in cluster_groups.items():
            for cluster in clusters:
                super_clusters[label].extend(cluster.tour.objects)

        # Now, we get the list of segment pairs that will communicate through
        # the current cluster. This does not include the segments within the
        # current cluster, as those will be accounted for separately.
        # Data (Sx, Sy)
        sc_index_pairs = itertools.permutations(super_clusters.keys(), 2)
        segment_pairs = list()
        for src_sc_index, dst_sc_index in sc_index_pairs:
            src_segments = super_clusters[src_sc_index]
            dst_segments = super_clusters[dst_sc_index]
            segment_pairs.extend(
                list(itertools.product(src_segments, dst_segments)))

        intercluster_volume = np.sum([segment_volume(src, dst, self.env)
                                      for src, dst in segment_pairs])

        # Data(Ss, Sd)
        if not intercluster_only:
            # NOW we calculate the intra-cluster volume
            segment_pairs = itertools.permutations(
                current_cluster.tour.objects, 2)
            intracluster_volume = np.sum([segment_volume(src, dst, self.env)
                                          for src, dst in
                                          segment_pairs])
        else:
            intracluster_volume = 0

        # ... and the outgoing data volume from this cluster
        # Data(Sy, Sx)
        other_segments = list(
            set(self.sim.segments) - set(current_cluster.tour.objects))
        segment_pairs = itertools.product(current_cluster.tour.objects,
                                          other_segments)
        intercluster_volume += np.sum([segment_volume(s, d, self.env)
                                       for s, d in segment_pairs])

        return intercluster_volume + intracluster_volume

    def total_comms_energy(self, cluster_id):

        if cluster_id in self._ids_to_comms_energy:
            return self._ids_to_comms_energy[cluster_id]

        data_volume = self.cluster_data_volume(cluster_id)
        energy = data_volume * self.env.comms_cost
        self._ids_to_comms_energy[cluster_id] = energy

        return energy

    def _find_cluster(self, cluster_id):
        """

        :param cluster_id:
        :return:
        :rtype: core.cluster.BaseCluster
        """
        if cluster_id in self._ids_to_clusters:
            return self._ids_to_clusters[cluster_id]

        found_cluster = None
        for clust in self.sim.clusters:
            if clust.cluster_id == cluster_id:
                found_cluster = clust
                break

        if not found_cluster:
            raise LOAFEnergyModelError(
                "Could not find cluster {}".format(cluster_id))

        self._ids_to_clusters[cluster_id] = found_cluster
        return found_cluster

    def total_movement_energy(self, cluster_id):
        """
        Return the amount of energy required to complete a single tour of the
        specified cluster.

        :param cluster_id: The numeric identifier of the cluster.

        :return: The amount of energy required.
        :rtype: pq.J
        """

        if cluster_id in self._ids_to_movement_energy:
            return self._ids_to_movement_energy[cluster_id]

        current_cluster = self._find_cluster(cluster_id)
        energy = current_cluster.tour_length * self.env.move_cost
        self._ids_to_movement_energy[cluster_id] = energy
        return energy

    def total_energy(self, cluster_id):
        """
        Get the sum of communication and movement energy for the given cluster.

        :param cluster_id:
        :return:
        :rtype: pq.J
        """

        energy = self.total_comms_energy(cluster_id)    # Ec(Mi)
        energy += self.total_movement_energy(cluster_id)    # Em(Mi)
        return energy
