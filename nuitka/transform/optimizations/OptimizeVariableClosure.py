#     Copyright 2012, Kay Hayen, mailto:kayhayen@gmx.de
#
#     Part of "Nuitka", an optimizing Python compiler that is compatible and
#     integrates with CPython, but also works on its own.
#
#     If you submit patches or make the software available to licensors of
#     this software in either form, you automatically them grant them a
#     license for your part of the code under "Apache License 2.0" unless you
#     choose to remove this notice.
#
#     Kay Hayen uses the right to license his code under only GPL version 3,
#     to discourage a fork of Nuitka before it is "finished". He will later
#     make a new "Nuitka" release fully under "Apache License 2.0".
#
#     This program is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, version 3 of the License.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#     Please leave the whole of this copyright notice intact.
#
""" Variable closure taking.

Run away, don't read it, quick. Heavily underdocumented rules are implemented here.

"""

from .OptimizeBase import OptimizationVisitorScopedBase

# TODO: The variable closure thing looks like it should be collapsed into tree building,
# this does not at all depend on completion of the node tree. Just the not early closure
# kinds need to make an extra pass, once their body is complete, in order to assign the
# variable.

class VariableClosureLookupVisitorPhase2( OptimizationVisitorScopedBase ):
    """ Variable closure phase 2: Find assignments and early closure references.

        In class context, a reference to a variable must be obeyed immediately, so
        that "variable = variable" takes first "variable" as a closure and then adds
        a new local "variable" to override it from there on. For the not early closure
        case of a function, this will not be done and only assigments shall add local
        variables, and references be ignored until phase 3.
    """

    def onEnterNode( self, node ):
        if node.isExpressionTargetVariableRef():
            if node.getVariable() is None:
                provider = node.getParentVariableProvider()

                node.setVariable(
                    provider.getVariableForAssignment(
                        variable_name = node.getVariableName()
                    )
                )
        elif node.isExpressionVariableRef():
            if node.getVariable() is None:
                provider = node.getParentVariableProvider()

                if provider.isEarlyClosure():
                    node.setVariable(
                        provider.getVariableForReference(
                            variable_name = node.getVariableName()
                        )
                    )




class VariableClosureLookupVisitorPhase3( OptimizationVisitorScopedBase ):
    def onEnterNode( self, node ):
        if node.isExpressionVariableRef() and node.getVariable() is None:
            provider = node.getParentVariableProvider()

            # print "Late reference", node.getVariableName(), "for", provider, "caused at", node, "of", node.getParent()

            node.setVariable(
                provider.getVariableForReference(
                    variable_name = node.getVariableName()
                )
            )

VariableClosureLookupVisitors = (
    VariableClosureLookupVisitorPhase2,
    VariableClosureLookupVisitorPhase3
)
