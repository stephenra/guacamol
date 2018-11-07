import logging
from typing import Optional, List, Iterable

import numpy as np
from rdkit import Chem, RDLogger, DataStructs
from rdkit.Chem import AllChem

from guacamol.utils.data import remove_duplicates

# Mute RDKit logger
RDLogger.logger().setLevel(RDLogger.CRITICAL)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def is_valid(smiles: str):
    """
    Verifies whether a SMILES string corresponds to a valid molecule.

    Args:
        smiles: SMILES string

    Returns:
        True if the SMILES strings corresponds to a valid, non-empty molecule.
    """

    mol = Chem.MolFromSmiles(smiles)

    return smiles != '' and mol is not None and mol.GetNumAtoms() > 0


def canonicalize(smiles: str, include_stereocenters=True) -> Optional[str]:
    """
    Canonicalize the SMILES strings with RDKit.

    The algorithm is detailed under https://pubs.acs.org/doi/full/10.1021/acs.jcim.5b00543

    Args:
        smiles: SMILES string to canonicalize
        include_stereocenters: whether to keep the stereochemical information in the canonical SMILES string

    Returns:
        Canonicalized SMILES string, None if the molecule is invalid.
    """

    mol = Chem.MolFromSmiles(smiles)

    if mol is not None:
        return Chem.MolToSmiles(mol, isomericSmiles=include_stereocenters)
    else:
        return None


def canonicalize_list(smiles_list: Iterable[str], include_stereocenters=True) -> List[str]:
    """
    Canonicalize a list of smiles. Filters out repetitions and removes corrupted molecules.

    Args:
        smiles_list: molecules as SMILES strings
        include_stereocenters: whether to keep the stereochemical information in the canonical SMILES strings

    Returns:
        The canonicalized and filtered input smiles.
    """

    canonicalized_smiles = [canonicalize(smiles, include_stereocenters) for smiles in smiles_list]

    # Remove None elements
    canonicalized_smiles = [s for s in canonicalized_smiles if s is not None]

    return remove_duplicates(canonicalized_smiles)


def smiles_to_rdkit_mol(smiles: str) -> Optional[Chem.Mol]:
    """
    Converts a SMILES string to a RDKit molecule.

    Args:
        smiles: SMILES string of the molecule

    Returns:
        RDKit Mol, None if the SMILES string is invalid
    """
    mol = Chem.MolFromSmiles(smiles)

    #  Sanitization check (detects invalid valence)
    if mol is not None:
        try:
            Chem.SanitizeMol(mol)
        except ValueError:
            return None

    return mol


def split_charged_mol(smiles: str) -> str:
    if smiles.count('.') > 0:
        largest = ''
        largest_len = -1
        split = smiles.split('.')
        for i in split:
            if len(i) > largest_len:
                largest = i
                largest_len = len(i)
        return largest

    else:
        return smiles


def initialise_neutralisation_reactions():
    patts = (
        # Imidazoles
        ('[n+;H]', 'n'),
        # Amines
        ('[N+;!H0]', 'N'),
        # Carboxylic acids and alcohols
        ('[$([O-]);!$([O-][#7])]', 'O'),
        # Thiols
        ('[S-;X1]', 'S'),
        # Sulfonamides
        ('[$([N-;X2]S(=O)=O)]', 'N'),
        # Enamines
        ('[$([N-;X2][C,N]=C)]', 'N'),
        # Tetrazoles
        ('[n-]', '[nH]'),
        # Sulfoxides
        ('[$([S-]=O)]', 'S'),
        # Amides
        ('[$([N-]C=O)]', 'N'),
    )
    return [(Chem.MolFromSmarts(x), Chem.MolFromSmiles(y, False)) for x, y in patts]


def neutralise_charges(mol, reactions=None):
    replaced = False

    for i, (reactant, product) in enumerate(reactions):
        while mol.HasSubstructMatch(reactant):
            replaced = True
            rms = AllChem.ReplaceSubstructs(mol, reactant, product)
            mol = rms[0]
    if replaced:
        Chem.SanitizeMol(mol)
        return mol, True
    else:
        return mol, False


def filter_and_canonicalize(smiles: str, holdout_set, holdout_fps, neutralization_rxns, tanimoto_cutoff=0.5,
                            include_stereocenters=False):
    """
    Args:
        smiles: the molecule to process
        holdout_set: smiles of the holdout set
        holdout_fps: ECFP4 fingerprints of the holdout set
        neutralization_rxns: neutralization rdkit reactions
        tanimoto_cutoff: Remove molecules with a higher ECFP4 tanimoto similarity than this cutoff from the set
        include_stereocenters: whether to keep stereocenters during canonicalization

    Returns:
        list with canonical smiles as a list with one element, or a an empty list. This is to perform a flatmap:
    """
    try:
        # Drop out if too long
        if len(smiles) > 200:
            return []
        mol = Chem.MolFromSmiles(smiles)
        # Drop out if invalid
        if mol is None:
            return []
        mol = Chem.RemoveHs(mol)

        # We only accept molecules consisting of H, B, C, N, O, F, Si, P, S, Cl, aliphatic Se, Br, I.
        metal_smarts = Chem.MolFromSmarts('[!#1!#5!#6!#7!#8!#9!#14!#15!#16!#17!#34!#35!#53]')

        has_metal = mol.HasSubstructMatch(metal_smarts)

        # Exclude molecules containing the forbidden elements.
        if has_metal:
            print(f'metal {smiles}')
            return []

        canon_smi = Chem.MolToSmiles(mol, isomericSmiles=include_stereocenters)

        # Drop out if too long canonicalized:
        if len(canon_smi) > 100:
            return []
        # Balance charges if unbalanced
        if canon_smi.count('+') - canon_smi.count('-') != 0:
            new_mol, changed = neutralise_charges(mol, reactions=neutralization_rxns)
            if changed:
                mol = new_mol
                canon_smi = Chem.MolToSmiles(mol, isomericSmiles=include_stereocenters)

        # Get most similar to holdout fingerprints, and exclude too similar molecules.
        max_tanimoto = highest_tanimoto_precalc_fps(mol, holdout_fps)
        if max_tanimoto < tanimoto_cutoff and canon_smi not in holdout_set:
            return [canon_smi]
        else:
            print("Exclude: {} {}".format(canon_smi, max_tanimoto))
    except Exception as e:
        print(e)
    return []


def get_internal_similarities(smiles_list: List[str]):
    if len(smiles_list) > 4096:
        logger.warning(f'Calculating internal similarity on large set of '
                       f'SMILES strings ({len(smiles_list)}')

    mols = get_mols(smiles_list)
    fps = get_fingerprints(mols)
    nfps = len(fps)

    similarities: List[float] = []

    for i in range(1, nfps):
        sims = DataStructs.BulkTanimotoSimilarity(fps[i], fps[:i])
        similarities.extend(sims)

    similarities = np.array(similarities).flatten()

    return similarities


def get_similarities_against(smiles_list1: List[str], smiles_list2: List[str]):
    if len(smiles_list1) > 4096 or len(smiles_list2) > 4096:
        logger.warning(f'Calculating similarity between large sets of '
                       f'SMILES strings ({len(smiles_list1)} x {len(smiles_list2)})')

    mols1 = get_mols(smiles_list1)
    fps1 = get_fingerprints(mols1)

    mols2 = get_mols(smiles_list2)
    fps2 = get_fingerprints(mols2)

    similarities = []

    for fp1 in fps1:
        sims = DataStructs.BulkTanimotoSimilarity(fp1, fps2)

        similarities.append(sims)

    similarities = np.array(similarities)

    return similarities


def get_fingerprints_from_smileslist(smiles_list):
    """
    Converts the provided smiles into ECFP4 bitvectors of length 4096.
    Args:
        smiles_list: list of SMILES strings

    Returns: ECFP4 bitvectors of length 4096.

    """
    return get_fingerprints(get_mols(smiles_list))


def get_fingerprints(mols: Iterable[Chem.Mol], radius=2, length=4096):
    """
    Converts molecules to ECFP bitvectors.
    Args:
        mols: RDKit molecules
        radius: ECFP fingerprint radius
        length: number of bits

    Returns: a list of fingerprints
    """
    return [AllChem.GetMorganFingerprintAsBitVect(m, radius, length) for m in mols]


def get_mols(smiles_list: Iterable[str]) -> Iterable[Chem.Mol]:
    for i in smiles_list:
        try:
            mol = Chem.MolFromSmiles(i)
            if mol is not None:
                yield mol
        except Exception as e:
            print(e)


def highest_tanimoto_precalc_fps(mol, fps):
    """

    Args:
        mol: Rdkit molecule
        fps: precalculated ECFP4 bitvectors

    Returns:

    """

    if fps is None or len(fps) == 0:
        return 0

    fp1 = AllChem.GetMorganFingerprintAsBitVect(mol, 2, 4096)
    sims = np.array(DataStructs.BulkTanimotoSimilarity(fp1, fps))

    return sims.max()