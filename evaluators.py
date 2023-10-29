import os
import warnings
from abc import ABC, abstractmethod

import useful_rdkit_utils as uru

try:
    from openeye import oechem
    from openeye import oeomega
    from openeye import oeshape
    from openeye import oedocking
except ImportError:
    # Since openeye is a commercial software package, just pass with a warning if not available
    warnings.warn(f"Openeye packages not available in this environment; do not attempt to use ROCSEvaluator or "
                  f"FredEvaluator")
from rdkit import Chem, DataStructs
import pandas as pd


class Evaluator(ABC):
    @abstractmethod
    def evaluate(self, mol):
        pass

    @property
    @abstractmethod
    def counter(self):
        pass


class MWEvaluator(Evaluator):
    """A simple evaluation class that calculates molecular weight, this was just a development tool
    """

    def __init__(self):
        self.num_evaluations = 0

    @property
    def counter(self):
        return self.num_evaluations

    def evaluate(self, mol):
        self.num_evaluations += 1
        return uru.MolWt(mol)


class FPEvaluator(Evaluator):
    """An evaluator class that calculates a fingerprint Tanimoto to a reference molecule
    """

    def __init__(self, input_dict):
        self.ref_smiles = input_dict["query_smiles"]
        self.ref_fp = uru.smi2morgan_fp(self.ref_smiles)
        self.num_evaluations = 0

    @property
    def counter(self):
        return self.num_evaluations

    def evaluate(self, rd_mol_in):
        self.num_evaluations += 1
        rd_mol_fp = uru.mol2morgan_fp(rd_mol_in)
        return DataStructs.TanimotoSimilarity(self.ref_fp, rd_mol_fp)


class ROCSEvaluator(Evaluator):
    """An evaluator class that calculates a ROCS score to a reference molecule
    """

    def __init__(self, input_dict):
        ref_filename = input_dict['query_molfile']
        ref_fs = oechem.oemolistream(ref_filename)
        self.ref_mol = oechem.OEMol()
        oechem.OEReadMolecule(ref_fs, self.ref_mol)
        self.max_confs = 50
        self.score_cache = {}
        self.num_evaluations = 0

    @property
    def counter(self):
        return self.num_evaluations

    def set_max_confs(self, max_confs):
        """Set the maximum number of conformers generated by Omega
        :param max_confs:
        """
        self.max_confs = max_confs

    def evaluate(self, rd_mol_in):
        """Generate conformers with Omega and evaluate the ROCS overlay of conformers to a reference molecule
        :param rd_mol_in: Input RDKit molecule
        :return: ROCS Tanimoto Combo score, returns -1 if conformer generation fails
        """
        self.num_evaluations += 1
        smi = Chem.MolToSmiles(rd_mol_in)
        # Look up to see if we already processed this molecule
        arc_tc = self.score_cache.get(smi)
        if arc_tc is not None:
            tc = arc_tc
        else:
            fit_mol = oechem.OEMol()
            oechem.OEParseSmiles(fit_mol, smi)
            ret_code = generate_confs(fit_mol, self.max_confs)
            if ret_code:
                tc = self.overlay(fit_mol)
            else:
                tc = -1.0
            self.score_cache[smi] = tc
        return tc

    def overlay(self, fit_mol):
        """Use ROCS to overlay two molecules
        :param fit_mol: OEMolecule
        :return: Combo Tanimoto for the overlay
        """
        prep = oeshape.OEOverlapPrep()
        prep.Prep(self.ref_mol)
        overlay = oeshape.OEMultiRefOverlay()
        overlay.SetupRef(self.ref_mol)
        prep.Prep(fit_mol)
        score = oeshape.OEBestOverlayScore()
        overlay.BestOverlay(score, fit_mol, oeshape.OEHighestTanimoto())
        return score.GetTanimotoCombo()


class LookupEvaluator(Evaluator):
    """A simple evaluation class that looks up values from a file.
    This is primarily used for testing.
    """

    def __init__(self, input_dictionary):
        self.num_evaluations = 0
        ref_filename = input_dictionary['ref_filename']
        ref_df = pd.read_csv(ref_filename)
        self.ref_dict = dict([(a, b) for a, b in ref_df[['SMILES', 'val']].values])

    @property
    def counter(self):
        return self.num_evaluations

    def evaluate(self, mol):
        self.num_evaluations += 1
        smi = Chem.MolToSmiles(mol)
        return self.ref_dict[smi]


class FredEvaluator(Evaluator):
    """An evaluator class that docks a molecule with the OEDocking Toolkit and returns the score
    """

    def __init__(self, input_dict):
        du_file = input_dict["design_unit_file"]
        if not os.path.isfile(du_file):
            raise FileNotFoundError(f"{du_file} was not found or is a directory")
        self.dock = read_design_unit(du_file)
        self.num_evaluations = 0
        self.max_confs = 50

    @property
    def counter(self):
        return self.num_evaluations

    def set_max_confs(self, max_confs):
        """Set the maximum number of conformers generated by Omega
        :param max_confs:
        """
        self.max_confs = max_confs

    def evaluate(self, mol):
        self.num_evaluations += 1
        smi = Chem.MolToSmiles(mol)
        mc_mol = oechem.OEMol()
        oechem.OEParseSmiles(mc_mol, smi)
        confs_ok = generate_confs(mc_mol, self.max_confs)
        score = 1000.0
        docked_mol = oechem.OEGraphMol()
        if confs_ok:
            ret_code = self.dock.DockMultiConformerMolecule(docked_mol, mc_mol)
        else:
            ret_code = oedocking.OEDockingReturnCode_ConformerGenError
        if ret_code == oedocking.OEDockingReturnCode_Success:
            dock_opts = oedocking.OEDockOptions()
            sd_tag = oedocking.OEDockMethodGetName(dock_opts.GetScoreMethod())
            # this is a stupid hack, I need to figure out how to do this correctly
            oedocking.OESetSDScore(docked_mol, self.dock, sd_tag)
            score = float(oechem.OEGetSDData(docked_mol, sd_tag))
        return score


def generate_confs(mol, max_confs):
    """Generate conformers with Omega
    :param max_confs: maximum number of conformers to generate
    :param mol: input OEMolecule
    :return: Boolean Omega return code indicating success of conformer generation
    """
    rms = 0.5
    strict_stereo = False
    omega = oeomega.OEOmega()
    omega.SetRMSThreshold(rms)  # Word to the wise: skipping this step can lead to significantly different charges!
    omega.SetStrictStereo(strict_stereo)
    omega.SetMaxConfs(max_confs)
    error_level = oechem.OEThrow.GetLevel()
    # Turn off OEChem warnings
    oechem.OEThrow.SetLevel(oechem.OEErrorLevel_Error)
    status = omega(mol)
    # Turn OEChem warnings back on
    oechem.OEThrow.SetLevel(error_level)
    return status


def read_design_unit(filename):
    """Read an OpenEye design unit
    :param filename: design unit filename (.oedu)
    :return: a docking grid
    """
    du = oechem.OEDesignUnit()
    rfs = oechem.oeifstream()
    if not rfs.open(filename):
        oechem.OEThrow.Fatal("Unable to open %s for reading" % filename)

    du = oechem.OEDesignUnit()
    if not oechem.OEReadDesignUnit(rfs, du):
        oechem.OEThrow.Fatal("Failed to read design unit")
    if not du.HasReceptor():
        oechem.OEThrow.Fatal("Design unit %s does not contain a receptor" % du.GetTitle())
    dock_opts = oedocking.OEDockOptions()
    dock = oedocking.OEDock(dock_opts)
    dock.Initialize(du)
    return dock


def test_fred_eval():
    """Test function for the Fred docking Evaluator
    :return: None
    """
    fred_eval = FredEvaluator("data/2zdt_receptor.oedu")
    smi = "CCSc1ncc2c(=O)n(-c3c(C)nc4ccccn34)c(-c3[nH]nc(C)c3F)nc2n1"
    mol = Chem.MolFromSmiles(smi)
    score = fred_eval.evaluate(mol)
    print(score)


def test_rocs_eval():
    """Test function for the ROCS evaluator
    :return: None
    """
    rocs_eval = ROCSEvaluator("data/2chw_lig.sdf")
    smi = "CCSc1ncc2c(=O)n(-c3c(C)nc4ccccn34)c(-c3[nH]nc(C)c3F)nc2n1"
    mol = Chem.MolFromSmiles(smi)
    combo_score = rocs_eval.evaluate(mol)
    print(combo_score)


if __name__ == "__main__":
    test_fred_eval()
