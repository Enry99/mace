import torch
from typing import Dict, List
from os import sys
import torch
import numpy as np
from CG_coefficients.CG_lorentz import CGDict
from functions.Zonal_functions import ZonalFunctions
from Cutoff import PolynomialCutoff
from Radial_basis import BesselBasis
from torch_scatter import scatter


class LinearNodeEmbeddingBlock(torch.nn.Module):
    def __init__(self, num_in: int, num_out: int):
        super().__init__()
        self.linear = torch.nn.Linear(num_in, num_out)

    def forward(
            self,
            node_attrs: torch.Tensor,  # [n_nodes, 1]
    ):
        return self.linear(node_attrs)

class NonLinearBlock(torch.nn.Module):
    def __init__(self, gate : torch.nn.Module):
        super().__init__()
        self.non_linearity = gate

    def forward(
            self,
            x: torch.Tensor  # [n_nodes, 1]
    ) -> torch.Tensor:  # [..., ]
        return self.gate(x)  # [n_nodes, 1]

class AtomicEnergiesBlock(torch.nn.Module):
    atomic_energies: torch.Tensor

    def __init__(self, atomic_energies: Union[np.ndarray, torch.Tensor]):
        super().__init__()
        assert len(atomic_energies.shape) == 1

        self.register_buffer('atomic_energies', torch.tensor(atomic_energies,
                                                             dtype=torch.get_default_dtype()))  # [n_elements, ]

    def forward(
            self,
            x: torch.Tensor  # one-hot of elements [..., n_elements]
    ) -> torch.Tensor:  # [..., ]
        return torch.matmul(x, self.atomic_energies)

    def __repr__(self):
        formatted_energies = ', '.join([f'{x:.4f}' for x in self.atomic_energies])
        return f'{self.__class__.__name__}(energies=[{formatted_energies}])'

class RadialEmbeddingBlock(torch.nn.Module):
    def __init__(self, r_max: float, num_bessel: int, num_polynomial_cutoff: int):
        super().__init__()
        self.bessel_fn = BesselBasis(r_max=r_max, num_basis=num_bessel)
        self.cutoff_fn = PolynomialCutoff(r_max=r_max, p=num_polynomial_cutoff)
        self.out_dim = num_bessel

    def forward(
            self,
            edge_lengths: torch.Tensor,  # [n_edges, 1]
    ):
        bessel = self.bessel_fn(edge_lengths)  # [n_edges, n_basis]
        cutoff = self.cutoff_fn(edge_lengths)  # [n_edges, 1]
        return bessel * cutoff  # [n_edges, n_basis]

class EdgeEmbeddingBlock(torch.nn.Module):
    def __init__(self,
                 lmax: int,
                 r_cut: float,
                 nmax: int = 8,
                 num_polynomial_cutoff: int = 6,
                 ):
        super().__init__()
        self.linear_radial = torch.nn.Linear(nmax,nmax)
        
    def forward(
            self,
            edge_index : torch.Tensor, 
            radial_feats : List[torch.Tensor], # [n_edges, num_basis]
            edge_attrs : List[torch.Tensor],  # [n_edges, 3]
            node_attrs : torch.Tensor,      
    ) -> List[torch.tensor,torch.tensor]:
        sender, receiver = edge_index
        r_size = radial_feats.size()
        radial_feats = self.linear_radial(radial_feats)
        combined_r = torch.einsum('bi,bk,bj -> bkij', radial_feats.view(r_size[0],r_size[-1])
                                                    ,node_attrs[sender],edge_attrs)  # [n_edges, n_basis , lmax*2 + 2*lmax +1] real part of radial embedding
        combined_i = torch.einsum('bi,bk,bj -> bkij', radial_feats.view(r_size[0],r_size[-1])
                                                    ,node_attrs[sender],edge_attrs)  # [n_edges, n_basis , lmax*2 + 2*lmax +1] imag part of radial embedding
        return (combined_r, combined_i) # [n_edges, n_basis , lmax*2 + 2*lmax +1]

class AtomicBaseBlock(torch.nn.Module):
    """ Create the Atomic base from pooling 1-particle basis"""
    def __init__(self, ):
        super().__init__()

    def forward(self, 
                edge_index: torch.tensor,
                radial_feature: torch.tensor,
                node_feats: torch.tensor,) -> List[torch.tensor,torch.tensor]:
            
        sender, receiver = edge_index  # The graph connectivity
        num_nodes = node_feats.shape[0]
        combined_r = torch.einsum(
                'bkij,bl -> bkij',radial_feature[0],node_feats[sender])  # [n_edges,n_species, n_basis , lmax*2 + 2*lmax +1] real part of radial embedding
        combined_i = torch.einsum(
                'bkij,bl -> bkij',radial_feature[1],node_feats[sender])  # [n_edges,n_species, n_basis , lmax*2 + 2*lmax +1] imag part of radial embedding
        edge_feats = (combined_r, combined_i)
        A_nlm_real = scatter(edge_feats[0], index=receiver, dim=0, dim_size=num_nodes,
                             reduce='sum')  #size [num_nodes,n,lmax**2 + 2*lmax + 1]
        A_nlm_imag = scatter(edge_feats[1], index=receiver, dim=0, dim_size=num_nodes,
                             reduce='sum')  #size [num_nodes,n,lmax**2 + 2*lmax + 1]
        node_feats = (A_nlm_real, A_nlm_imag)
        return node_feats, edge_feats

class VectorizeBlock(torch.nn.Module):
    def __init__(self,
                c_tildes_dict : Dict[str,torch.Tensor],
                 device = 'cpu'):
        super().__init__()
        #Create the dict or pass it? For correlation 4 can be very long
        self.max_corr = c_tildes_dict['degree'].max_corr() 
        contract_module = OrderedDict()
        for vu in range(self.max_corr,1,-1) :  
          contract_module[f"contract_{vu}"]  = tensor_contract_nd_update_sparse(
                                                              c_tildes_dict[vu],
                                                              correlation=vu,
                                                              device = device)
        contract_module["vector_contract"] = vector_contract()
        self.contract = torch.nn.Sequential(contract_module) 
         
    def forward(self,
                atomic_basis, #atomic basis for one atom and one species
                c_tildes_dict_w): #c_tilde weighter for the corresponding element
        
        A_z = {'atomic_basis' : [atomic_basis[0].flatten().unsqueeze(1),atomic_basis[1].flatten().unsqueeze(1)],
               'c_tildes_dict_w' : c_tildes_dict_w} #hack needs to be removed 
        A_v = self.contract(A_z)['a_update']
        return A_v